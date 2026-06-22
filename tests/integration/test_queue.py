import pytest_asyncio
from sqlalchemy import text

from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


@pytest_asyncio.fixture
async def sf(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_enqueue_e_claim(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.INDEX_DOCUMENT.value, {"repo_path": "a.md"})
    job = await q.claim_next("w1")
    assert job is not None
    assert job.id == jid
    assert job.payload == {"repo_path": "a.md"}
    assert job.attempts == 1


async def test_claim_vazio_retorna_none(sf):
    q = PostgresJobQueue(sf)
    assert await q.claim_next("w1") is None


async def test_skip_locked_nao_entrega_o_mesmo_job(sf):
    q = PostgresJobQueue(sf)
    await q.enqueue(JobType.INDEX_DOCUMENT.value, {"n": 1})
    j1 = await q.claim_next("w1")
    j2 = await q.claim_next("w2")  # só existe 1 job pendente
    assert j1 is not None
    assert j2 is None


async def test_fail_reenfileira_ate_o_limite(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.INDEX_DOCUMENT.value, {})
    # 1a tentativa
    await q.claim_next("w1")
    await q.fail(jid, "boom", max_attempts=2)
    # volta para pending, mas só fica elegível após o backoff
    assert await q.claim_next("w1") is None
    async with sf() as s:
        await s.execute(
            text("UPDATE ingestion_jobs SET run_after = now() - interval '1 second' WHERE id=:id"),
            {"id": jid},
        )
        await s.commit()
    job = await q.claim_next("w1")
    assert job is not None and job.attempts == 2
    await q.fail(jid, "boom2", max_attempts=2)
    # agora atingiu o limite -> failed, não reaparece
    assert await q.claim_next("w1") is None
    async with sf() as s:
        status = (await s.execute(
            text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": jid}
        )).scalar_one()
    assert status == "failed"


async def test_fail_define_backoff_exponencial(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.INDEX_DOCUMENT.value, {})
    await q.claim_next("w1")
    await q.fail(jid, "boom", max_attempts=3)

    async with sf() as s:
        run_after = (await s.execute(
            text("SELECT run_after FROM ingestion_jobs WHERE id=:id"),
            {"id": jid},
        )).scalar_one()

    assert run_after is not None
    assert await q.claim_next("w2") is None


async def test_complete_marca_done(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.REINDEX.value, {})
    await q.claim_next("w1")
    await q.complete(jid)
    async with sf() as s:
        status = (await s.execute(
            text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": jid}
        )).scalar_one()
    assert status == "done"
