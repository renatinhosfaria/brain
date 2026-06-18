import hashlib
import hmac
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
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
        if not authorization.startswith(prefix) or not authorization[len(prefix):]:
            await self._unauthorized(send)
            return

        principal_token = None
        async with self.sf() as session:
            try:
                principal = await auth.resolve_principal(
                    session, self.settings, authorization[len(prefix):]
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
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [(b"content-type", b"text/plain")],
        })
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
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict:
        async with sf() as s:
            rows = (await s.execute(
                text("SELECT status, count(*) FROM ingestion_jobs GROUP BY status")
            )).all()
        return {"jobs": {r[0]: r[1] for r in rows}}

    @app.post("/webhook/github")
    async def webhook(request: Request):
        body = await request.body()
        if not verify_signature(
            settings.webhook_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            return Response(status_code=401)
        before, after = git_sync.clone_or_pull(
            settings.repo_url, settings.repo_cache_path, settings.github_token
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
