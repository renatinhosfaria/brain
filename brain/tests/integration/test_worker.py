import pytest_asyncio

from brain.config import Settings
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base
from brain.worker import run_once


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 2000 for _ in texts]


class FakeLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {"entities": [], "relations": []}
        return {"facts": [{"content": "usa python", "confidence": 0.9}]}


@pytest_asyncio.fixture
async def ctx(async_dsn, tmp_path):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    settings = Settings(
        database_url=async_dsn, openai_api_key="x", github_token="x",
        brain_auth_token="x", webhook_secret="x", repo_url="x",
        repo_cache_path=str(tmp_path),
    )
    yield sf, PostgresJobQueue(sf), settings, tmp_path
    await engine.dispose()


async def test_worker_processa_extract_facts(ctx):
    sf, queue, settings, _ = ctx
    await queue.enqueue(JobType.EXTRACT_FACTS.value, {
        "namespace": "p", "messages": [{"role": "user", "content": "eu uso python"}]
    })
    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is True
    async with sf() as s:
        mems = await repo.list_memories(s, "p")
    assert len(mems) == 1


async def test_worker_processa_index_document(ctx):
    sf, queue, settings, tmp = ctx
    (tmp / "a.md").write_text("# Nota\ncorpo", encoding="utf-8")
    await queue.enqueue(JobType.INDEX_DOCUMENT.value, {"namespace": "t", "repo_path": "a.md"})
    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is True
    async with sf() as s:
        doc = await repo.get_document(s, repo_path="a.md")
    assert doc is not None


async def test_worker_job_desconhecido_vai_para_failed(ctx):
    sf, queue, settings, _ = ctx
    jid = await queue.enqueue("tipo_invalido", {})
    # esgota tentativas
    for _ in range(settings.max_job_attempts):
        await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings)
    from sqlalchemy import text
    async with sf() as s:
        status = (await s.execute(
            text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": jid}
        )).scalar_one()
    assert status == "failed"


async def test_run_once_sem_jobs_retorna_false(ctx):
    sf, queue, settings, _ = ctx
    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is False
