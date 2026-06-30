import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from brain import auth
from brain.config import get_settings
from brain.extraction.llm import LLMClient
from brain.indexing.embeddings import Embedder
from brain.ingestion import git_sync
from brain.mcp.handlers import Deps
from brain.mcp.server import create_mcp_server
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.repo_paths import normalize_repo_path
from brain.storage.db import make_engine, make_session_factory

log = structlog.get_logger()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _is_brain_bot_push(payload: dict, *, name: str, email: str) -> bool:
    commits = payload.get("commits") or []
    if not commits:
        return False
    for commit in commits:
        author = commit.get("author") or {}
        if author.get("name") != name and author.get("email") != email:
            return False
    return True


class _PrincipalAuth:
    """Middleware ASGI que protege o app MCP montado."""

    def __init__(self, app: ASGIApp, sf, settings) -> None:
        self.app = app
        self.sf = sf
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode()
        prefix = "Bearer "
        if not authorization.startswith(prefix) or not authorization[len(prefix) :]:
            await self._unauthorized(send)
            return

        principal_token = None
        async with self.sf() as session:
            try:
                principal = await auth.resolve_principal(
                    session, self.settings, authorization[len(prefix) :]
                )
            except auth.AuthError:
                await session.rollback()
                await self._unauthorized(send)
                return

            try:
                principal_token = auth.set_current_principal(principal)
                await self.app(scope, receive, send)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                if principal_token is not None:
                    auth.reset_current_principal(principal_token)

    async def _unauthorized(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"Unauthorized"})


def build_deps(settings):
    engine = make_engine(settings.database_url)
    sf = make_session_factory(engine)
    deps = Deps(
        sf,
        Embedder.from_settings(settings),
        LLMClient.from_settings(settings),
        PostgresJobQueue(sf),
        settings,
    )
    return deps, sf


def create_app(deps: Deps, sf) -> FastAPI:
    settings = deps.settings
    mcp = create_mcp_server(deps)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="brain", lifespan=lifespan)

    @app.get("/health")
    async def health():
        try:
            async with sf() as s:
                await s.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            log.warning("health_database_unavailable", error=str(exc))
            return JSONResponse(
                {"status": "error", "database": "unavailable"},
                status_code=503,
            )
        return {"status": "ok", "database": "ok"}

    @app.get("/status")
    async def status(request: Request):
        if request.headers.get("Authorization") != f"Bearer {settings.brain_auth_token}":
            return Response(status_code=401)
        async with sf() as s:
            rows = (
                await s.execute(text("SELECT status, count(*) FROM ingestion_jobs GROUP BY status"))
            ).all()
            failed_rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id, type, attempts, last_error "
                            "FROM ingestion_jobs WHERE status='failed' "
                            "ORDER BY updated_at DESC LIMIT 20"
                        )
                    )
                )
                .mappings()
                .all()
            )
        job_counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        job_counts.update({r[0]: r[1] for r in rows})
        return {
            "jobs": job_counts,
            "failed_jobs": [
                {
                    "id": str(row["id"]),
                    "type": row["type"],
                    "attempts": row["attempts"],
                    "last_error": row["last_error"],
                }
                for row in failed_rows
            ],
        }

    @app.post("/webhook/github")
    async def webhook(request: Request):
        body = await request.body()
        if not verify_signature(
            settings.webhook_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            return Response(status_code=401)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        event = request.headers.get("X-GitHub-Event")
        if event and event != "push":
            return {"enqueued": 0, "ignored": True}
        if _is_brain_bot_push(
            payload,
            name=settings.git_author_name,
            email=settings.git_author_email,
        ):
            return {"enqueued": 0, "ignored": True}
        before, after = git_sync.clone_or_pull(
            settings.repo_url,
            settings.repo_cache_path,
            settings.github_token,
            committer_name=settings.git_author_name,
            committer_email=settings.git_author_email,
        )
        enqueued = 0
        for code, path in git_sync.changed_files(settings.repo_cache_path, before, after):
            try:
                repo_path, _ = normalize_repo_path(
                    settings.repo_cache_path, path, require_markdown=True
                )
            except ValueError as e:
                log.warning("webhook_skipped_repo_path", path=path, error=str(e))
                continue
            if code == "D":
                await deps.queue.enqueue(JobType.DELETE_DOCUMENT.value, {"repo_path": repo_path})
            else:
                await deps.queue.enqueue(
                    JobType.INDEX_DOCUMENT.value,
                    {"namespace": "curated", "repo_path": repo_path, "commit_sha": after},
                )
            enqueued += 1
        return {"enqueued": enqueued}

    app.mount("/mcp", _PrincipalAuth(mcp_app, sf, settings))
    return app


# App de produção (uvicorn brain.main:app). Só constrói se houver env configurado.
app = create_app(*build_deps(get_settings())) if os.getenv("DATABASE_URL") else None
