import hashlib
import hmac

import anyio
import pytest_asyncio
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from brain import main
from brain.config import Settings
from brain.main import build_deps, create_app
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


@pytest_asyncio.fixture
async def prepared_db(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


def _settings(async_dsn, tmp_path) -> Settings:
    return Settings(
        database_url=async_dsn, openai_api_key="x", github_token="x",
        brain_auth_token="tok", brain_curator_token="curator-token",
        brain_token_encryption_key=Fernet.generate_key().decode(),
        webhook_secret="seg", repo_url="https://x/y.git",
        repo_cache_path=str(tmp_path), git_push_enabled=False,
    )


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


def test_health(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_health_retorna_503_quando_banco_indisponivel(tmp_path):
    settings = _settings("postgresql+asyncpg://brain:brain@127.0.0.1:1/brain", tmp_path)
    app = create_app(*build_deps(settings))
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "error", "database": "unavailable"}


def test_status_retorna_contagem(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        body = client.get("/status", headers=_auth_headers()).json()
    assert body["jobs"] == {"pending": 0, "running": 0, "done": 0, "failed": 0}


def test_status_rejeita_sem_bearer(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        r = client.get("/status")
    assert r.status_code == 401


def test_status_lista_jobs_failed(async_dsn, tmp_path, prepared_db):
    async def seed_failed_job():
        engine = make_engine(async_dsn)
        sf = make_session_factory(engine)
        queue = PostgresJobQueue(sf)
        job_id = await queue.enqueue(JobType.INDEX_DOCUMENT.value, {"namespace": "p"})
        await queue.claim_next("worker-1")
        await queue.fail(job_id, "boom", max_attempts=1)
        await engine.dispose()
        return job_id

    job_id = anyio.run(seed_failed_job)
    deps, sf = build_deps(_settings(async_dsn, tmp_path))
    app = create_app(deps, sf)
    with TestClient(app) as client:
        body = client.get("/status", headers=_auth_headers()).json()

    assert body["jobs"]["failed"] == 1
    assert body["failed_jobs"] == [
        {
            "id": str(job_id),
            "type": JobType.INDEX_DOCUMENT.value,
            "attempts": 1,
            "last_error": "boom",
        }
    ]


def test_webhook_rejeita_assinatura_invalida(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=b"{}", headers={"X-Hub-Signature-256": "sha256=x"})
    assert r.status_code == 401


def test_webhook_rejeita_json_invalido(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b"{"
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_json"}


def test_webhook_rejeita_payload_nao_objeto(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b"[]"
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_payload"}


def test_mcp_streamable_http_exposto_em_mcp(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }
    headers = {
        "Authorization": "Bearer curator-token",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app, base_url="http://localhost:8000") as client:
        r = client.post("/mcp", json=payload, headers=headers)
    assert r.status_code == 200
    assert "serverInfo" in r.text


def test_webhook_enfileira_jobs(async_dsn, tmp_path, prepared_db, monkeypatch):
    monkeypatch.setattr(main.git_sync, "clone_or_pull", lambda *a, **k: ("old", "new"))
    monkeypatch.setattr(
        main.git_sync, "changed_files",
        lambda *a, **k: [("A", "trabalho/nota.md"), ("D", "trabalho/old.md")],
    )
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b'{"ref":"refs/heads/main"}'
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.json() == {"enqueued": 2}


def test_webhook_ignora_evento_nao_push(async_dsn, tmp_path, prepared_db, monkeypatch):
    def fail_clone(*args, **kwargs):
        pytest.fail("clone_or_pull nao deve ser chamado para eventos que nao sejam push")

    monkeypatch.setattr(main.git_sync, "clone_or_pull", fail_clone)
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b'{"zen":"Keep it logically awesome."}'
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post(
            "/webhook/github",
            content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"},
        )
    assert r.json() == {"enqueued": 0, "ignored": True}


def test_webhook_ignora_commits_do_brain_bot(async_dsn, tmp_path, prepared_db, monkeypatch):
    def fail_clone(*args, **kwargs):
        pytest.fail("clone_or_pull nao deve ser chamado para commits do brain-bot")

    monkeypatch.setattr(main.git_sync, "clone_or_pull", fail_clone)
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = (
        b'{"commits":[{"author":{"name":"brain-bot",'
        b'"email":"brain-bot@users.noreply.github.com"}}]}'
    )
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.json() == {"enqueued": 0, "ignored": True}
