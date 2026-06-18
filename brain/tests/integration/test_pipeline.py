import pytest_asyncio
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
        brain_auth_token="x", webhook_secret="x", repo_url="x",
    )


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 2000 for _ in texts]


class FakeLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {"entities": [{"name": "brain", "type": "projeto"}], "relations": []}
        return {"facts": [{"content": "gosta de python", "confidence": 0.8}]}


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


async def test_extract_and_store_facts(session):
    facts = await pipeline.extract_and_store_facts(
        session, FakeEmbedder(), FakeLLM(), namespace="p",
        messages=[{"role": "user", "content": "eu uso python"}],
    )
    assert facts[0]["content"] == "gosta de python"
    mems = await repo.list_memories(session, "p")
    assert len(mems) == 1
