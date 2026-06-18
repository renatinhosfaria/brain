import hashlib
import hmac
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from brain import auth
from brain import main
from brain.config import Settings
from brain.main import build_deps, create_app
from brain.queue.base import JobType
from brain.storage import repositories as repo
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
        brain_auth_token="tok", webhook_secret="seg", repo_url="https://x/y.git",
        repo_cache_path=str(tmp_path), git_push_enabled=False,
        brain_curator_token="curator-token",
    )


async def _queued_jobs(async_dsn):
    engine = make_engine(async_dsn)
    sf = make_session_factory(engine)
    async with sf() as session:
        rows = (
            await session.execute(
                text("SELECT type, payload FROM ingestion_jobs ORDER BY created_at, id")
            )
        ).mappings().all()
    await engine.dispose()
    return rows


@pytest_asyncio.fixture
async def principal_auth_ctx(async_dsn, tmp_path):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    settings = _settings(async_dsn, tmp_path)
    active_token = "active-client-token"
    disabled_token = "disabled-client-token"

    async with sf() as session:
        await repo.create_agent_client(
            session,
            slug="codex",
            name="Codex",
            description=None,
            token_prefix="brain_client_codex",
            token_hash=auth.hash_token(active_token),
            token_encrypted="encrypted-active",
            permissions=["search"],
            meta=None,
        )
        await repo.create_agent_client(
            session,
            slug="disabled",
            name="Disabled",
            description=None,
            token_prefix="brain_client_disabled",
            token_hash=auth.hash_token(disabled_token),
            token_encrypted="encrypted-disabled",
            permissions=["search"],
            meta=None,
        )
        await repo.disable_agent_client(session, "disabled")
        await session.commit()

    yield SimpleNamespace(
        sf=sf,
        settings=settings,
        active_token=active_token,
        disabled_token=disabled_token,
    )
    await engine.dispose()


def _principal_echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def principal():
        current = auth.get_current_principal()
        return {"type": current.type, "slug": current.slug, "name": current.name}

    return app


async def _request_principal(ctx, bearer_token: str) -> httpx.Response:
    app = main._PrincipalAuth(_principal_echo_app(), ctx.sf, ctx.settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/", headers={"Authorization": f"Bearer {bearer_token}"})


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


async def test_webhook_ignora_alteracoes_em_agents(async_dsn, tmp_path, prepared_db, monkeypatch):
    monkeypatch.setattr(main.git_sync, "clone_or_pull", lambda *a, **k: ("old", "new"))
    monkeypatch.setattr(
        main.git_sync,
        "changed_files",
        lambda *a, **k: [
            ("A", "_agents"),
            ("A", "./_agents/codex/raw.md"),
            ("A", "_agents/codex/raw.md"),
            ("M", "_agents\\codex\\raw.md"),
            ("M", "x/../_agents/codex/raw.md"),
            ("M", "_agents/chatgpt-web/nota.md"),
            ("A", "trabalho/nota.md"),
        ],
    )
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b'{"ref":"refs/heads/main"}'
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})

    assert r.json() == {"enqueued": 1}
    rows = await _queued_jobs(async_dsn)
    assert [row["payload"]["repo_path"] for row in rows] == ["trabalho/nota.md"]


async def test_webhook_enfileira_markdown_curado_com_namespace_curated(
    async_dsn, tmp_path, prepared_db, monkeypatch
):
    monkeypatch.setattr(main.git_sync, "clone_or_pull", lambda *a, **k: ("old", "new"))
    monkeypatch.setattr(
        main.git_sync,
        "changed_files",
        lambda *a, **k: [("A", ".\\projetos\\brain.md")],
    )
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b'{"ref":"refs/heads/main"}'
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})

    assert r.json() == {"enqueued": 1}
    rows = await _queued_jobs(async_dsn)
    assert [row["type"] for row in rows] == [JobType.INDEX_DOCUMENT.value]
    assert rows[0]["payload"] == {
        "namespace": "curated",
        "repo_path": "projetos/brain.md",
        "commit_sha": "new",
    }


async def test_webhook_ignora_loop_de_symlink_sem_500(
    async_dsn, tmp_path, prepared_db, monkeypatch
):
    (tmp_path / "loop.md").symlink_to("loop.md")
    monkeypatch.setattr(main.git_sync, "clone_or_pull", lambda *a, **k: ("old", "new"))
    monkeypatch.setattr(
        main.git_sync,
        "changed_files",
        lambda *a, **k: [("A", "loop.md"), ("A", "trabalho/nota.md")],
    )
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b'{"ref":"refs/heads/main"}'
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})

    assert r.status_code == 200
    assert r.json() == {"enqueued": 1}
    rows = await _queued_jobs(async_dsn)
    assert [row["payload"]["repo_path"] for row in rows] == ["trabalho/nota.md"]


def test_mcp_rota_publica_existe_com_auth_valida(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    headers = {"Authorization": "Bearer curator-token", "host": "localhost"}

    def get_following_redirect(client: TestClient, path: str):
        response = client.get(path, headers=headers, follow_redirects=False)
        if response.status_code in {307, 308}:
            response = client.get(
                response.headers["location"], headers=headers, follow_redirects=False
            )
        return response

    with TestClient(app) as client:
        for path in ("/mcp", "/mcp/"):
            response = get_following_redirect(client, path)
            assert response.status_code != 401
            assert response.status_code != 404


async def test_resolve_principal_accepts_client_token(principal_auth_ctx):
    async with principal_auth_ctx.sf() as session:
        principal = await auth.resolve_principal(
            session, principal_auth_ctx.settings, principal_auth_ctx.active_token
        )
        await session.commit()

    assert principal == auth.Principal(type="client", slug="codex", name="Codex")

    async with principal_auth_ctx.sf() as session:
        client = await repo.get_agent_client(session, slug="codex")
    assert client.last_seen_at is not None


async def test_resolve_principal_rejects_disabled_client_token(principal_auth_ctx):
    async with principal_auth_ctx.sf() as session:
        with pytest.raises(auth.AuthError):
            await auth.resolve_principal(
                session, principal_auth_ctx.settings, principal_auth_ctx.disabled_token
            )


async def test_resolve_principal_rejects_unknown_token(principal_auth_ctx):
    async with principal_auth_ctx.sf() as session:
        with pytest.raises(auth.AuthError):
            await auth.resolve_principal(session, principal_auth_ctx.settings, "unknown-token")


async def test_principal_auth_accepts_curator_token(principal_auth_ctx):
    response = await _request_principal(principal_auth_ctx, "curator-token")

    assert response.status_code == 200
    assert response.json() == {"type": "curator", "slug": "hermes", "name": "Hermes"}


async def test_principal_auth_accepts_client_token_and_commits_last_seen(principal_auth_ctx):
    response = await _request_principal(principal_auth_ctx, principal_auth_ctx.active_token)

    assert response.status_code == 200
    assert response.json() == {"type": "client", "slug": "codex", "name": "Codex"}

    async with principal_auth_ctx.sf() as session:
        client = await repo.get_agent_client(session, slug="codex")
    assert client.last_seen_at is not None


async def test_principal_auth_rejects_disabled_client_token(principal_auth_ctx):
    response = await _request_principal(principal_auth_ctx, principal_auth_ctx.disabled_token)

    assert response.status_code == 401
    assert response.text == "Unauthorized"


async def test_principal_auth_rejects_unknown_token(principal_auth_ctx):
    response = await _request_principal(principal_auth_ctx, "unknown-token")

    assert response.status_code == 401
    assert response.text == "Unauthorized"
