import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from brain import auth
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
        brain_token_encryption_key=Fernet.generate_key().decode(),
    )
    yield Deps(sf, FakeEmbedder(), None, PostgresJobQueue(sf), settings)
    await engine.dispose()


async def _as_principal(principal: auth.Principal, fn, *args, **kwargs):
    token = auth.set_current_principal(principal)
    try:
        return await fn(*args, **kwargs)
    finally:
        auth.reset_current_principal(token)


async def _create_client_as_curator(deps, **kwargs):
    return await _as_principal(
        auth.Principal("curator", "hermes", "Hermes"),
        handlers.create_agent_client,
        deps,
        **({"name": "ChatGPT Web"} | kwargs),
    )


def _assert_safe_client(client: dict) -> None:
    assert "token" not in client
    assert "token_hash" not in client
    assert "token_encrypted" not in client
    assert client["token_prefix"]


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


async def test_curator_cria_agent_client_e_recebe_token_uma_vez(deps):
    out = await _create_client_as_curator(
        deps,
        description="Cliente usado pelo ChatGPT.",
        capture_policy="Capture somente fatos persistentes.",
        recommended_instructions="Use search antes de submeter notas.",
        metadata={"owner": "Hermes"},
    )

    assert out["slug"] == "chatgpt-web"
    assert out["name"] == "ChatGPT Web"
    assert out["token"].startswith("brain_client_chatgpt-web_")
    assert out["token"].startswith(out["token_prefix"])
    assert len(out["token_prefix"]) < len(out["token"])
    assert out["permissions"] == ["search", "get_note", "submit_agent_note"]

    principal = auth.Principal("curator", "hermes", "Hermes")
    clients = await _as_principal(principal, handlers.list_agent_clients, deps)
    got = await _as_principal(principal, handlers.get_agent_client, deps, "chatgpt-web")

    assert [client["slug"] for client in clients] == ["chatgpt-web"]
    _assert_safe_client(clients[0])
    _assert_safe_client(got)


async def test_agent_client_profile_existe_sem_token_completo(deps):
    out = await _create_client_as_curator(deps)

    profile = Path(deps.settings.repo_cache_path) / out["profile_path"]
    assert profile.exists()
    text = profile.read_text(encoding="utf-8")

    assert out["profile_path"] == "_agents/chatgpt-web/chatgpt-web.md"
    assert out["token_prefix"] in text
    assert out["token"] not in text


async def test_reveal_agent_client_token_retorna_plaintext_apenas_para_curator(deps):
    created = await _create_client_as_curator(deps)

    revealed = await _as_principal(
        auth.Principal("curator", "hermes", "Hermes"),
        handlers.reveal_agent_client_token,
        deps,
        "chatgpt-web",
    )

    assert revealed["slug"] == "chatgpt-web"
    assert revealed["token"] == created["token"]
    assert revealed["token_prefix"] == created["token_prefix"]

    with pytest.raises(PermissionError, match="curator required"):
        await _as_principal(
            auth.Principal("client", "chatgpt-web", "ChatGPT Web"),
            handlers.reveal_agent_client_token,
            deps,
            "chatgpt-web",
        )


async def test_rotate_agent_client_token_troca_token_e_invalida_hash_antigo(deps):
    created = await _create_client_as_curator(deps, slug="codex", name="Codex")
    async with deps.session_factory() as s:
        before = await repo.get_agent_client(s, slug="codex")
        old_hash = before.token_hash

    rotated = await _as_principal(
        auth.Principal("curator", "hermes", "Hermes"),
        handlers.rotate_agent_client_token,
        deps,
        "codex",
    )

    assert rotated["slug"] == "codex"
    assert rotated["token"] != created["token"]
    assert rotated["token"].startswith(rotated["token_prefix"])
    async with deps.session_factory() as s:
        assert await repo.get_agent_client_by_token_hash(s, old_hash) is None
        after = await repo.get_agent_client(s, slug="codex")
        assert after.token_hash != old_hash
        assert after.token_prefix == rotated["token_prefix"]


async def test_disable_agent_client_altera_status(deps):
    await _create_client_as_curator(deps, slug="codex", name="Codex")

    out = await _as_principal(
        auth.Principal("curator", "hermes", "Hermes"),
        handlers.disable_agent_client,
        deps,
        "codex",
    )

    assert out["disabled"] is True
    assert out["status"] == "disabled"
    async with deps.session_factory() as s:
        assert (await repo.get_agent_client(s, slug="codex")).status == "disabled"


async def test_principal_client_nao_gerencia_agent_clients(deps):
    await _create_client_as_curator(deps)
    principal = auth.Principal("client", "chatgpt-web", "ChatGPT Web")

    calls = [
        (handlers.create_agent_client, (deps,), {"name": "Codex"}),
        (handlers.list_agent_clients, (deps,), {}),
        (handlers.get_agent_client, (deps, "chatgpt-web"), {}),
        (handlers.reveal_agent_client_token, (deps, "chatgpt-web"), {}),
        (handlers.rotate_agent_client_token, (deps, "chatgpt-web"), {}),
        (handlers.disable_agent_client, (deps, "chatgpt-web"), {}),
    ]

    for fn, args, kwargs in calls:
        with pytest.raises(PermissionError, match="curator required"):
            await _as_principal(principal, fn, *args, **kwargs)
