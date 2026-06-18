import pytest
import pytest_asyncio

from brain.graph import age
from brain.search.retriever import search
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


def _vec(seed: float) -> list[float]:
    # Direção que varia com `seed` (a distância de cosseno ignora magnitude).
    return [1.0, seed] + [0.0] * 1998


class FakeEmbedder:
    def __init__(self, mapping):
        self._m = mapping

    async def embed(self, texts):
        return [self._m[t] for t in texts]


async def _add_document_chunk(session, *, namespace: str, repo_path: str, text: str, seed: float):
    doc = await repo.upsert_document(
        session,
        namespace=namespace,
        repo_path=repo_path,
        title=None,
        raw_content=text,
        content_hash=f"h-{repo_path}",
        commit_sha=None,
    )
    await repo.replace_chunks(
        session,
        doc.id,
        [{"ordinal": 0, "text": text, "token_count": 1}],
        [_vec(seed)],
    )
    return doc


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        await age.ensure_graph(s)
        from sqlalchemy import text
        await age._prepare(s)
        await s.execute(text(
            "SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)"
        ))
        await s.commit()
        yield s
    await engine.dispose()


async def test_search_retorna_chunks_de_notas_curadas(session):
    doc = await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await search(session, emb, "consulta", limit=10)

    assert out["results"] == [
        {
            "id": str(doc.id),
            "text": "nota curada sobre brain",
            "score": out["results"][0]["score"],
            "source": "document",
            "ref": "projetos/brain.md",
            "path": "projetos/brain.md",
            "repo_path": "projetos/brain.md",
            "namespace": "curated",
        }
    ]


async def test_search_nao_retorna_memorias_ou_agents(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada visivel",
        seed=0.30,
    )
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="_agents/chatgpt-web/raw.md",
        text="nota bruta de agente",
        seed=0.10,
    )
    await _add_document_chunk(
        session,
        namespace="legacy",
        repo_path="conversas/legacy.md",
        text="documento legado",
        seed=0.12,
    )
    await repo.add_memory(
        session,
        namespace="curated",
        content="memoria legada proxima",
        embedding=_vec(0.11),
    )
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await search(session, emb, "consulta", limit=10)

    assert [r["text"] for r in out["results"]] == ["nota curada visivel"]
    assert {r["source"] for r in out["results"]} == {"document"}
    assert all(not r["repo_path"].startswith("_agents/") for r in out["results"])


async def test_search_path_prefix_limita_resultados(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota de projeto",
        seed=0.90,
    )
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="areas/trabalho.md",
        text="nota de area",
        seed=0.10,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await search(
        session,
        emb,
        "consulta",
        limit=10,
        filters={"path_prefix": "projetos/"},
    )

    assert [r["repo_path"] for r in out["results"]] == ["projetos/brain.md"]


@pytest.mark.parametrize(
    ("path_prefix", "match"),
    [
        ("../", "path_prefix"),
        ("_agents/", "_agents"),
        ("%", "path_prefix"),
        ("projetos/_", "path_prefix"),
    ],
)
async def test_search_rejeita_path_prefix_invalido(session, path_prefix, match):
    emb = FakeEmbedder({"consulta": _vec(0.11)})

    with pytest.raises(ValueError, match=match):
        await search(session, emb, "consulta", filters={"path_prefix": path_prefix})


@pytest.mark.parametrize("limit", [0, -1, True, False, "10", 1.5])
async def test_search_rejeita_limit_invalido(session, limit):
    emb = FakeEmbedder({"consulta": _vec(0.11)})

    with pytest.raises(ValueError, match="limit"):
        await search(session, emb, "consulta", limit=limit)


async def test_search_limita_limit_muito_alto(session):
    for idx in range(55):
        await _add_document_chunk(
            session,
            namespace="curated",
            repo_path=f"projetos/nota-{idx:02d}.md",
            text=f"nota curada {idx}",
            seed=0.10 + idx / 10000,
        )
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await search(session, emb, "consulta", limit=10_000)

    assert len(out["results"]) == 50
    assert {r["namespace"] for r in out["results"]} == {"curated"}


async def test_include_graph_traz_relacionados(session):
    await age.upsert_entity(session, "brain", "projeto", "t")
    await age.upsert_entity(session, "Renato", "pessoa", "t")
    await age.upsert_relation(session, "brain", "Renato", "owned_by", "t")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.5)})
    out = await search(session, emb, "brain", namespace="t", include_graph=True)
    assert any(g["name"] == "Renato" for g in out["graph"])
