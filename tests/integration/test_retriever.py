import pytest
import pytest_asyncio

from brain.graph import age
from brain.search import retriever
from brain.search.retriever import deep_search, search
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


class FakeLLM:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"entities": []}
        self.error = error
        self.calls = 0

    async def complete_json(self, system, user):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


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


async def test_deep_search_nao_retorna_memorias_ou_agents_em_resultados_textuais(session):
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
    await repo.add_memory(
        session,
        namespace="curated",
        content="memoria legada proxima",
        embedding=_vec(0.11),
    )
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await deep_search(session, emb, FakeLLM({"entities": []}), "consulta", limit=10)

    assert [r["text"] for r in out["results"]] == ["nota curada visivel"]
    assert {r["source"] for r in out["results"]} == {"document"}
    assert all(not r["repo_path"].startswith("_agents/") for r in out["results"])


async def test_search_exclui_agents_sem_excluir_xagents(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="_agents/foo.md",
        text="nota bruta de agente",
        seed=0.10,
    )
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="xagents/foo.md",
        text="nota curada com prefixo parecido",
        seed=0.11,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.10)})
    out = await search(session, emb, "consulta", limit=10)

    assert [r["repo_path"] for r in out["results"]] == ["xagents/foo.md"]


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


async def test_search_nao_retorna_memorias_mesmo_com_filtro_source_memory(session):
    doc = await repo.upsert_document(
        session, namespace="curated", repo_path="a.md", title=None,
        raw_content="x", content_hash="h", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id, [{"ordinal": 0, "text": "doc perto", "token_count": 1}], [_vec(0.10)]
    )
    await repo.add_memory(session, namespace="t", content="mem perto", embedding=_vec(0.11))
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.10)})
    out = await search(
        session,
        emb,
        "consulta",
        namespace="t",
        limit=10,
        filters={"source": "memory"},
    )

    assert out["results"] == []


async def test_include_graph_traz_relacionados(session):
    await age.upsert_entity(session, "brain", "projeto", "t")
    await age.upsert_entity(session, "Renato", "pessoa", "t")
    await age.upsert_relation(session, "brain", "Renato", "owned_by", "t")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.5)})
    out = await search(session, emb, "brain", namespace="t", include_graph=True)
    assert any(g["name"] == "Renato" for g in out["graph"])


async def test_deep_search_combina_chunks_e_grafo_por_fast_path(session):
    doc = await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.11)})
    out = await deep_search(session, emb, None, "brain", limit=10, depth=1)

    assert out["query"] == "brain"
    assert out["results"][0]["id"] == str(doc.id)
    assert out["graph"]["relationships"] == [
        {"from": "Hermes", "to": "brain", "type": "curates", "seed": "brain", "depth": 1}
    ]
    entities = {entity["name"]: entity for entity in out["graph"]["entities"]}
    assert entities["brain"]["matched_by"] == "substring"
    assert entities["Hermes"]["matched_by"] == "relationship"
    assert out["meta"]["seed_strategy"] == "substring"


async def test_resolve_seed_entities_passa_limit_para_busca_direta(monkeypatch):
    calls = []

    async def fake_search_entities(session, query, namespace, limit=None):
        calls.append((query, namespace, limit))
        return [
            {"name": "Brain Zeta", "type": "projeto"},
            {"name": "Brain Alpha", "type": "projeto"},
            {"name": "Brain Beta", "type": "projeto"},
        ]

    monkeypatch.setattr(retriever.age, "search_entities", fake_search_entities)

    seeds, strategy = await retriever._resolve_seed_entities(None, "brain", "curated", 2)

    assert calls == [("brain", "curated", 2)]
    assert strategy == "substring"
    assert [seed["name"] for seed in seeds] == ["Brain Zeta", "Brain Alpha"]


async def test_deep_search_seleciona_seeds_diretos_em_ordem_deterministica_e_limitada(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    for seed_name, target_name in [
        ("Brain Zeta", "Target Zeta"),
        ("Brain Alpha", "Target Alpha"),
        ("Brain Beta", "Target Beta"),
    ]:
        await age.upsert_entity(session, seed_name, "projeto", "curated")
        await age.upsert_entity(session, target_name, "conceito", "curated")
        await age.upsert_relation(session, seed_name, target_name, "mentions", "curated")
    await session.commit()

    emb = FakeEmbedder({"Brain": _vec(0.11)})
    out = await deep_search(session, emb, None, "Brain", depth=1, max_entities=2)

    matched_seed_names = {
        entity["name"]
        for entity in out["graph"]["entities"]
        if entity.get("matched_by") == "substring"
    }
    assert matched_seed_names == {"Brain Alpha", "Brain Beta"}
    assert {rel["seed"] for rel in out["graph"]["relationships"]} == {
        "Brain Alpha",
        "Brain Beta",
    }


async def test_deep_search_propaga_falha_de_travessia_do_grafo(session, monkeypatch):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await session.commit()

    async def fail_get_relationship_paths(*args, **kwargs):
        raise RuntimeError("age traversal failed")

    monkeypatch.setattr(retriever.age, "get_relationship_paths", fail_get_relationship_paths)

    emb = FakeEmbedder({"brain": _vec(0.11)})
    with pytest.raises(RuntimeError, match="age traversal failed"):
        await deep_search(session, emb, None, "brain", depth=1)


async def test_deep_search_usa_fallback_llm_quando_substring_nao_encontra_seed(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await session.commit()

    emb = FakeEmbedder({"Como o projeto se relaciona com o curador?": _vec(0.11)})
    llm = FakeLLM({"entities": [{"name": "brain"}]})
    out = await deep_search(
        session,
        emb,
        llm,
        "Como o projeto se relaciona com o curador?",
        limit=10,
        depth=1,
    )

    assert out["meta"]["seed_strategy"] == "llm"
    assert [rel["type"] for rel in out["graph"]["relationships"]] == ["curates"]
    entities = {entity["name"]: entity for entity in out["graph"]["entities"]}
    assert entities["brain"]["matched_by"] == "llm"
    assert entities["Hermes"]["matched_by"] == "relationship"


async def test_deep_search_sem_seeds_retorna_chunks_e_grafo_vazio(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta abstrata": _vec(0.11)})
    out = await deep_search(session, emb, FakeLLM({"entities": []}), "consulta abstrata")

    assert out["results"]
    assert out["graph"] == {"entities": [], "relationships": []}
    assert out["meta"]["seed_strategy"] == "none"


async def test_deep_search_fallback_llm_falha_retorna_warning(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta abstrata": _vec(0.11)})
    out = await deep_search(
        session,
        emb,
        FakeLLM(error=RuntimeError("llm indisponivel")),
        "consulta abstrata",
    )

    assert out["results"]
    assert out["graph"] == {"entities": [], "relationships": []}
    assert out["meta"]["seed_strategy"] == "none"
    assert out["meta"]["warnings"] == ["query entity fallback failed: llm indisponivel"]


@pytest.mark.parametrize("max_entities", [False, True, 0, -1, "2", 1.5])
async def test_deep_search_max_entities_invalido_nao_expande_grafo(session, max_entities):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await session.commit()

    emb = FakeEmbedder({"consulta abstrata": _vec(0.11)})
    llm = FakeLLM({"entities": [{"name": "brain"}]})
    out = await deep_search(session, emb, llm, "consulta abstrata", max_entities=max_entities)

    assert out["results"]
    assert out["graph"] == {"entities": [], "relationships": []}
    assert out["meta"]["max_entities"] == 0
    assert out["meta"]["seed_strategy"] == "none"
    assert out["meta"]["warnings"] == []
    assert llm.calls == 0


async def test_deep_search_namespace_controla_so_grafo(session):
    doc = await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "tenant-b")
    await age.upsert_entity(session, "Hermes", "agente", "tenant-b")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "tenant-b")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.11)})
    out = await deep_search(session, emb, None, "brain", namespace="tenant-b", depth=1)

    assert out["results"][0]["id"] == str(doc.id)
    assert {result["namespace"] for result in out["results"]} == {"curated"}
    assert out["graph"]["relationships"] == [
        {"from": "Hermes", "to": "brain", "type": "curates", "seed": "brain", "depth": 1}
    ]
