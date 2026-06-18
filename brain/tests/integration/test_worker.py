import datetime as dt

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from brain.config import Settings
from brain.outbox import sign_webhook
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base, OutboxEvent
from brain.worker import run_once


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 2000 for _ in texts]


class FakeLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {"entities": [], "relations": []}
        return {"facts": [{"content": "usa python", "confidence": 0.9}]}


async def _get_outbox_event(session_factory, event_id):
    async with session_factory() as s:
        return (
            await s.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one()


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


async def test_worker_entrega_outbox_hermes_e_marca_delivered(ctx):
    sf, queue, settings, _ = ctx
    settings = settings.model_copy(
        update={
            "hermes_webhook_url": "https://hermes.test/events",
            "hermes_webhook_secret": "segredo",
        }
    )
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"status": "created", "note_id": "note-1"},
        )
        await s.commit()
        event_id = event.id

    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await run_once(
            sf,
            queue,
            FakeEmbedder(),
            FakeLLM(),
            settings,
            outbox_client=client,
        ) is True

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://hermes.test/events"
    assert request.content == b'{"note_id":"note-1","status":"created"}'
    assert request.headers["X-Brain-Event-Id"] == str(event_id)
    assert request.headers["X-Brain-Event-Type"] == "agent_note.created"
    timestamp = request.headers["X-Brain-Timestamp"]
    assert request.headers["X-Brain-Signature"] == sign_webhook(
        "segredo",
        timestamp,
        request.content,
    )

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "delivered"
    assert stored.attempts == 1
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_worker_outbox_hermes_reagenda_non_2xx(ctx):
    sf, queue, settings, _ = ctx
    settings = settings.model_copy(
        update={
            "hermes_webhook_url": "https://hermes.test/events",
            "hermes_webhook_secret": "segredo",
            "outbox_max_attempts": 3,
        }
    )
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"note_id": "note-1"},
        )
        await s.commit()
        event_id = event.id

    before_dispatch = dt.datetime.now(dt.UTC)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await run_once(
            sf,
            queue,
            FakeEmbedder(),
            FakeLLM(),
            settings,
            outbox_client=client,
        ) is True

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "retrying"
    assert stored.attempts == 1
    assert stored.last_error == "Hermes webhook returned HTTP 500"
    assert stored.run_after is not None
    assert stored.run_after > before_dispatch
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_worker_outbox_hermes_marca_failed_ao_esgotar_tentativas(ctx):
    sf, queue, settings, _ = ctx
    settings = settings.model_copy(
        update={
            "hermes_webhook_url": "https://hermes.test/events",
            "hermes_webhook_secret": "segredo",
            "outbox_max_attempts": 2,
        }
    )
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"note_id": "note-1"},
        )
        event.attempts = 1
        await s.commit()
        event_id = event.id

    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await run_once(
            sf,
            queue,
            FakeEmbedder(),
            FakeLLM(),
            settings,
            outbox_client=client,
        ) is True

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "failed"
    assert stored.attempts == 2
    assert stored.last_error == "Hermes webhook returned HTTP 503"
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_worker_outbox_sem_url_mantem_evento_pending(ctx):
    sf, queue, settings, _ = ctx
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"note_id": "note-1"},
        )
        await s.commit()
        event_id = event.id

    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is False

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "pending"
    assert stored.attempts == 0
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_worker_outbox_erro_inesperado_reagenda_e_limpa_lock(ctx):
    sf, queue, settings, _ = ctx
    settings = settings.model_copy(
        update={
            "hermes_webhook_url": "https://hermes.test/events",
            "hermes_webhook_secret": "segredo",
            "outbox_max_attempts": 3,
        }
    )
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"note_id": "note-1"},
        )
        await s.commit()
        event_id = event.id

    def handler(request):
        raise RuntimeError("cliente quebrou")

    before_dispatch = dt.datetime.now(dt.UTC)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await run_once(
            sf,
            queue,
            FakeEmbedder(),
            FakeLLM(),
            settings,
            outbox_client=client,
        ) is True

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "retrying"
    assert stored.attempts == 1
    assert stored.last_error == "Hermes webhook delivery error: cliente quebrou"
    assert stored.run_after is not None
    assert stored.run_after > before_dispatch
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_worker_outbox_reclama_running_stale(ctx):
    sf, queue, settings, _ = ctx
    settings = settings.model_copy(
        update={
            "hermes_webhook_url": "https://hermes.test/events",
            "hermes_webhook_secret": "segredo",
            "job_stale_seconds": 300,
        }
    )
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"note_id": "note-1"},
        )
        old_lock_time = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=301)
        claimed = await repo.claim_next_outbox_event(
            s,
            old_lock_time,
            worker_id="old-worker",
        )
        assert claimed.id == event.id
        await s.commit()
        event_id = event.id

    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await run_once(
            sf,
            queue,
            FakeEmbedder(),
            FakeLLM(),
            settings,
            worker_id="new-worker",
            outbox_client=client,
        ) is True

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "delivered"
    assert stored.attempts == 2
    assert stored.locked_at is None
    assert stored.locked_by is None


@pytest.mark.parametrize("secret", [None, ""])
async def test_worker_outbox_sem_secret_mantem_evento_pending(ctx, secret):
    sf, queue, settings, _ = ctx
    settings = settings.model_copy(
        update={
            "hermes_webhook_url": "https://hermes.test/events",
            "hermes_webhook_secret": secret,
        }
    )
    async with sf() as s:
        event = await repo.create_outbox_event(
            s,
            type="agent_note.created",
            payload={"note_id": "note-1"},
        )
        await s.commit()
        event_id = event.id

    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await run_once(
            sf,
            queue,
            FakeEmbedder(),
            FakeLLM(),
            settings,
            outbox_client=client,
        ) is False

    stored = await _get_outbox_event(sf, event_id)
    assert stored.status == "pending"
    assert stored.attempts == 0
    assert stored.locked_at is None
    assert stored.locked_by is None
