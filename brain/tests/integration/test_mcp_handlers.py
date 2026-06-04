import subprocess

import pytest_asyncio

from brain.config import Settings
from brain.mcp import handlers
from brain.mcp.handlers import Deps
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base
from brain.storage import repositories as repo


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.2] * 2000 for _ in texts]


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


@pytest_asyncio.fixture
async def deps(async_dsn, tmp_path):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    vault = tmp_path / "vault"
    _init_repo(vault)
    settings = Settings(
        database_url=async_dsn, openai_api_key="x", github_token="x",
        brain_auth_token="x", webhook_secret="x", repo_url="x",
        repo_cache_path=str(vault), git_push_enabled=False,
    )
    yield Deps(sf, FakeEmbedder(), None, PostgresJobQueue(sf), settings)
    await engine.dispose()


async def test_remember_grava_nota_e_enfileira(deps):
    out = await handlers.remember(deps, "trabalho", [{"role": "user", "content": "lembrar disso"}])
    assert out["note_path"].startswith("conversas/trabalho/")
    assert len(out["job_ids"]) == 2


async def test_namespaces_crud(deps):
    await handlers.create_namespace(deps, "t", "trabalho")
    names = [n["name"] for n in await handlers.list_namespaces(deps)]
    assert "t" in names


async def test_memoria_crud_via_handlers(deps):
    async with deps.session_factory() as s:
        m = await repo.add_memory(s, namespace="p", content="gosta de chá", embedding=[0.2] * 2000)
        await s.commit()
        mid = str(m.id)
    got = await handlers.get_memory(deps, mid)
    assert got["content"] == "gosta de chá"
    await handlers.move_memory(deps, mid, "trabalho")
    assert (await handlers.get_memory(deps, mid))["namespace"] == "trabalho"
    assert (await handlers.delete_memory(deps, mid))["deleted"] is True


async def test_reindex_enfileira(deps):
    out = await handlers.reindex(deps, "a.md", "t")
    assert "job_id" in out
