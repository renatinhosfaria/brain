import hashlib
import hmac

import pytest_asyncio
from fastapi.testclient import TestClient

from brain import main
from brain.config import Settings
from brain.main import build_deps, create_app
from brain.storage.db import make_engine
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
        brain_auth_token="tok", webhook_secret="seg", repo_url="https://x/y.git",
        repo_cache_path=str(tmp_path), git_push_enabled=False,
    )


def test_health(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_status_retorna_contagem(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        body = client.get("/status").json()
    assert "jobs" in body


def test_webhook_rejeita_assinatura_invalida(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=b"{}", headers={"X-Hub-Signature-256": "sha256=x"})
    assert r.status_code == 401


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
