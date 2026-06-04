import hashlib
import hmac
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from brain.config import get_settings
from brain.extraction.llm import LLMClient
from brain.indexing.embeddings import Embedder
from brain.ingestion import git_sync
from brain.mcp.handlers import Deps
from brain.mcp.server import create_mcp_server
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory

log = structlog.get_logger()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class _BearerAuth:
    """Middleware ASGI que protege o app MCP montado."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth != f"Bearer {self.token}":
                await send({
                    "type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


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
            namespace = path.split("/")[0]
            if code == "D":
                await deps.queue.enqueue(JobType.DELETE_DOCUMENT.value, {"repo_path": path})
            else:
                await deps.queue.enqueue(
                    JobType.INDEX_DOCUMENT.value,
                    {"namespace": namespace, "repo_path": path, "commit_sha": after},
                )
            enqueued += 1
        return {"enqueued": enqueued}

    app.mount("/mcp", _BearerAuth(mcp_app, settings.brain_auth_token))
    return app


# App de produção (uvicorn brain.main:app). Só constrói se houver env configurado.
app = create_app(*build_deps(get_settings())) if os.getenv("DATABASE_URL") else None
