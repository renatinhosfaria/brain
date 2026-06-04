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


async def test_busca_unifica_documentos_e_memorias_ordenado(session):
    doc = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title=None,
        raw_content="x", content_hash="h", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id, [{"ordinal": 0, "text": "doc perto", "token_count": 1}], [_vec(0.10)]
    )
    await repo.add_memory(session, namespace="t", content="mem longe", embedding=_vec(0.90))
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await search(session, emb, "consulta", namespace="t", limit=10)
    assert out["results"][0]["text"] == "doc perto"
    assert {r["source"] for r in out["results"]} == {"document", "memory"}


async def test_include_graph_traz_relacionados(session):
    await age.upsert_entity(session, "brain", "projeto", "t")
    await age.upsert_entity(session, "Renato", "pessoa", "t")
    await age.upsert_relation(session, "brain", "Renato", "owned_by", "t")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.5)})
    out = await search(session, emb, "brain", namespace="t", include_graph=True)
    assert any(g["name"] == "Renato" for g in out["graph"])
