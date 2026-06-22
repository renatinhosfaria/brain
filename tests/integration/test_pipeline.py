import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy import text

from brain.config import Settings
from brain.graph import age
from brain.ingestion import pipeline
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base, Chunk


def _settings() -> Settings:
    return Settings(
        database_url="x", openai_api_key="x", github_token="x",
        brain_auth_token="x", brain_curator_token="curator",
        brain_token_encryption_key=Fernet.generate_key().decode(),
        webhook_secret="x", repo_url="x",
    )


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 2000 for _ in texts]


class FakeLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {"entities": [{"name": "brain", "type": "projeto"}], "relations": []}
        return {"facts": [{"content": "gosta de python", "confidence": 0.8}]}


class SwitchingEntityLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            name = "Antigo" if "antigo" in user else "Novo"
            return {"entities": [{"name": name, "type": "conceito"}], "relations": []}
        return {"facts": []}


class SameNameCuratedLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {
                "entities": [
                    {
                        "name": "Stack técnica deve ser inferida por projeto",
                        "type": "conceito",
                    }
                ],
                "relations": [],
            }
        return {"facts": []}


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        await age.ensure_graph(s)
        await age._prepare(s)
        await s.execute(text(
            "SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)"
        ))
        await s.commit()
        yield s
    await engine.dispose()


async def test_index_document_cria_doc_chunks_e_entidades(session):
    created = await pipeline.index_document(
        session, FakeEmbedder(), FakeLLM(), _settings(),
        namespace="t", repo_path="a.md", content="# Nota\nconteúdo sobre brain", commit_sha="abc",
    )
    assert created is True
    doc = await repo.get_document(session, repo_path="a.md")
    assert doc is not None and doc.title == "Nota"
    ent = await age.get_entity(session, "brain", "t")
    assert ent is not None


async def test_index_document_sem_llm_indexa_doc_e_chunks_sem_entidades(session):
    created = await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        _settings(),
        namespace="curated",
        repo_path="projetos/brain.md",
        content="# Brain\n\nconteudo sobre brain",
        commit_sha=None,
    )

    assert created is True
    doc = await repo.get_document(session, repo_path="projetos/brain.md")
    chunks = list((await session.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars())
    ent = await age.get_entity(session, "brain", "curated")

    assert doc is not None
    assert doc.namespace == "curated"
    assert doc.title == "Brain"
    assert chunks
    assert ent is None


async def test_index_document_idempotente(session):
    args = dict(namespace="t", repo_path="a.md", content="# X\ncorpo", commit_sha=None)
    assert await pipeline.index_document(session, FakeEmbedder(), FakeLLM(), _settings(), **args) is True
    assert await pipeline.index_document(session, FakeEmbedder(), FakeLLM(), _settings(), **args) is False


async def test_index_document_mesmo_conteudo_atualiza_meta_commit_e_preserva_chunks(session):
    args = dict(namespace="t", repo_path="a.md", content="# X\ncorpo")
    assert await pipeline.index_document(
        session,
        FakeEmbedder(),
        FakeLLM(),
        _settings(),
        **args,
        commit_sha="old",
        meta={"version": 1},
    ) is True
    doc = await repo.get_document(session, repo_path="a.md")
    before_chunks = list(
        (await session.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars()
    )

    changed = await pipeline.index_document(
        session,
        FakeEmbedder(),
        FakeLLM(),
        _settings(),
        **args,
        commit_sha="new",
        meta={"version": 2},
    )
    after_doc = await repo.get_document(session, repo_path="a.md")
    after_chunks = list(
        (await session.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars()
    )

    assert changed is False
    assert after_doc.commit_sha == "new"
    assert after_doc.meta == {"version": 2}
    assert [chunk.id for chunk in after_chunks] == [chunk.id for chunk in before_chunks]


async def test_reindex_remove_entidades_antigas_do_mesmo_documento(session):
    settings = _settings()
    await pipeline.index_document(
        session, FakeEmbedder(), SwitchingEntityLLM(), settings,
        namespace="t", repo_path="a.md", content="# Nota\nantigo", commit_sha="old",
    )
    assert await age.get_entity(session, "Antigo", "t") is not None

    await pipeline.index_document(
        session, FakeEmbedder(), SwitchingEntityLLM(), settings,
        namespace="t", repo_path="a.md", content="# Nota\nnovo", commit_sha="new",
    )

    assert await age.get_entity(session, "Antigo", "t") is None
    assert await age.get_entity(session, "Novo", "t") is not None


async def test_index_document_cria_entidade_deterministica_de_nota_curada(session):
    content = "# Stack técnica deve ser inferida por projeto\n\nCorpo."

    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        _settings(),
        namespace="curated",
        repo_path="preferencias/stack-tecnica-por-projeto.md",
        content=content,
        commit_sha="abc",
        meta={"metadata": {"type": "preference", "tags": ["stack"]}},
    )

    found = await age.search_entities(session, "stack tecnica", "curated")
    assert found[0]["name"] == "Stack técnica deve ser inferida por projeto"
    ent = await age.get_entity(session, "Stack técnica deve ser inferida por projeto", "curated")
    assert ent["type"] == "preferencia"
    assert ent["props"]["source_doc"] == "preferencias/stack-tecnica-por-projeto.md"


async def test_index_document_entidade_deterministica_prevalece_sobre_llm_mesmo_nome(session):
    content = "# Stack técnica deve ser inferida por projeto\n\nCorpo."

    await pipeline.index_document(
        session,
        FakeEmbedder(),
        SameNameCuratedLLM(),
        _settings(),
        namespace="curated",
        repo_path="preferencias/stack-tecnica-por-projeto.md",
        content=content,
        commit_sha="abc",
        meta={
            "metadata": {
                "type": "preference",
                "tags": ["stack"],
                "aliases": ["stack por projeto"],
            }
        },
    )

    ent = await age.get_entity(session, "Stack técnica deve ser inferida por projeto", "curated")
    assert ent["type"] == "preferencia"
    assert ent["props"]["source"] == "curated_note"
    assert "stack" in ent["props"]["tags"]
    assert "stack por projeto" in ent["props"]["aliases"]


async def test_index_document_content_hash_igual_sincroniza_metadata_sem_rechunk(session):
    settings = _settings()
    content = "# Nome Antigo\n\nMesmo corpo."
    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        settings,
        namespace="curated",
        repo_path="preferencias/metadata.md",
        content=content,
        commit_sha="old",
        meta={"metadata": {"title": "Nome Antigo", "type": "preference"}},
    )

    changed = await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        settings,
        namespace="curated",
        repo_path="preferencias/metadata.md",
        content=content,
        commit_sha="new",
        meta={
            "metadata": {
                "title": "Nome Novo",
                "type": "decision",
                "tags": ["renomeado"],
            }
        },
    )

    assert changed is False
    assert await age.get_entity(session, "Nome Antigo", "curated") is None
    ent = await age.get_entity(session, "Nome Novo", "curated")
    assert ent is not None
    assert ent["type"] == "decisao"
    assert "renomeado" in ent["props"]["tags"]
    found = await age.search_entities(session, "renomeado", "curated")
    assert found[0]["name"] == "Nome Novo"


async def test_index_document_reindex_preserva_entidade_deterministica_e_relacoes(session):
    settings = _settings()
    repo_path = "preferencias/renomeavel.md"

    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        settings,
        namespace="curated",
        repo_path=repo_path,
        content="# Nome Antigo\n\nCorpo antigo.",
        commit_sha="old",
        meta={"metadata": {"title": "Nome Antigo", "type": "preference"}},
    )
    await age.upsert_entity(session, "Vizinho", "conceito", "curated")
    await age.upsert_relation(session, "Vizinho", "Nome Antigo", "relates_to", "curated")
    await session.commit()

    changed = await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        settings,
        namespace="curated",
        repo_path=repo_path,
        content="# Nome Novo\n\nCorpo novo.",
        commit_sha="new",
        meta={
            "metadata": {
                "title": "Nome Novo",
                "type": "decision",
                "tags": ["renomeado"],
            }
        },
    )

    assert changed is True
    assert await age.get_entity(session, "Nome Antigo", "curated") is None
    ent = await age.get_entity(session, "Nome Novo", "curated")
    assert ent["type"] == "decisao"
    assert ent["props"]["source"] == "curated_note"
    assert "renomeado" in ent["props"]["tags"]
    related = await age.get_related(session, "Nome Novo", "curated")
    assert {"name": "Vizinho", "type": "conceito"} in related


async def test_index_document_nao_cria_entidade_deterministica_para_agents(session):
    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        _settings(),
        namespace="curated",
        repo_path="_agents/chatgpt/raw.md",
        content="# Raw\n\nNao deve virar entidade.",
        commit_sha="abc",
    )

    assert await age.search_entities(session, "Raw", "curated") == []


async def test_index_document_agents_nao_prepara_grafo_para_sync_deterministico(
    session,
    monkeypatch,
):
    async def fail_ensure_graph(*args, **kwargs):
        raise AssertionError("ensure_graph nao deveria ser chamado para _agents sem LLM")

    monkeypatch.setattr(pipeline.age, "ensure_graph", fail_ensure_graph)

    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        _settings(),
        namespace="curated",
        repo_path="_agents/chatgpt/raw.md",
        content="# Raw\n\nNao deve virar entidade.",
        commit_sha="abc",
    )


async def test_search_entities_acceptance_queries_for_curated_note_aliases(session):
    settings = _settings()
    cases = [
        (
            "preferencias/stack-tecnica-por-projeto.md",
            "# Stack técnica deve ser inferida por projeto\n\nCorpo.",
            {"metadata": {"title": "Stack técnica deve ser inferida por projeto", "type": "preference"}},
            ["Stack técnica por projeto", "stack tecnica"],
            "Stack técnica deve ser inferida por projeto",
        ),
        (
            "preferencias/regras-env-e-migrations-por-projeto.md",
            "# Regras de .env e migrations dependem do projeto\n\nCorpo.",
            {"metadata": {"title": "Regras de .env e migrations dependem do projeto", "type": "preference"}},
            ["env migrations", "migrations por projeto"],
            "Regras de .env e migrations dependem do projeto",
        ),
        (
            "preferencias/privacidade-credenciais-e-acoes-externas.md",
            "# Privacidade, credenciais e ações externas\n\nCorpo.",
            {"metadata": {"title": "Privacidade, credenciais e ações externas", "type": "preference"}},
            ["Privacidade", "credenciais"],
            "Privacidade, credenciais e ações externas",
        ),
        (
            "preferencias/perfil-ceo.md",
            "# Perfil CEO\n\nCorpo.",
            {"metadata": {"title": "Perfil CEO", "aliases": ["Hermes CEO", "ceo hermes"]}},
            ["Hermes CEO"],
            "Perfil CEO",
        ),
        (
            "projetos/famaagent.md",
            "# FamaAgent\n\nProjeto.",
            {"metadata": {"title": "FamaAgent", "type": "project"}},
            ["FamaAgent"],
            "FamaAgent",
        ),
        (
            "projetos/mcp-fama.md",
            "# MCP-Fama\n\nProjeto.",
            {"metadata": {"title": "MCP-Fama", "type": "project", "aliases": ["mcp-fama"]}},
            ["mcp-fama"],
            "MCP-Fama",
        ),
        (
            "projetos/evolution-go.md",
            "# Evolution API\n\nProjeto.",
            {"metadata": {"title": "Evolution API", "type": "project", "aliases": ["Evolution-go"]}},
            ["Evolution-go"],
            "Evolution API",
        ),
        (
            "projetos/paperclip-openclaw.md",
            "# OpenClaw\n\nProjeto.",
            {"metadata": {"title": "OpenClaw", "type": "project", "aliases": ["Paperclip"]}},
            ["Paperclip"],
            "OpenClaw",
        ),
    ]

    for repo_path, content, meta, queries, _expected_name in cases:
        await pipeline.index_document(
            session,
            FakeEmbedder(),
            None,
            settings,
            namespace="curated",
            repo_path=repo_path,
            content=content,
            commit_sha="abc",
            meta=meta,
        )

    for _repo_path, _content, _meta, queries, expected_name in cases:
        for query in queries:
            found = await age.search_entities(session, query, "curated")
            assert found, query
            assert found[0]["name"] == expected_name
