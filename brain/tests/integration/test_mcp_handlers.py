import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select

from brain import auth
from brain.config import Settings
from brain.mcp import handlers
from brain.mcp.handlers import Deps
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base, OutboxEvent
from brain.storage import repositories as repo


CURATOR = auth.Principal("curator", "hermes", "Hermes")
CLIENT = auth.Principal("client", "chatgpt-web", "ChatGPT Web")


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


async def _as_curator(fn, *args, **kwargs):
    return await _as_principal(CURATOR, fn, *args, **kwargs)


async def _as_client(fn, *args, **kwargs):
    return await _as_principal(CLIENT, fn, *args, **kwargs)


async def _create_client_as_curator(deps, **kwargs):
    return await _as_curator(
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
    out = await _as_curator(
        handlers.remember, deps, "trabalho", [{"role": "user", "content": "lembrar disso"}]
    )
    assert out["note_path"].startswith("conversas/trabalho/")
    assert len(out["job_ids"]) == 2


async def test_namespaces_crud(deps):
    await _as_curator(handlers.create_namespace, deps, "t", "trabalho")
    names = [n["name"] for n in await _as_curator(handlers.list_namespaces, deps)]
    assert "t" in names


async def test_memoria_crud_via_handlers(deps):
    async with deps.session_factory() as s:
        m = await repo.add_memory(s, namespace="p", content="gosta de chá", embedding=[0.2] * 2000)
        await s.commit()
        mid = str(m.id)
    got = await _as_curator(handlers.get_memory, deps, mid)
    assert got["content"] == "gosta de chá"
    await _as_curator(handlers.move_memory, deps, mid, "trabalho")
    assert (await _as_curator(handlers.get_memory, deps, mid))["namespace"] == "trabalho"
    assert (await _as_curator(handlers.delete_memory, deps, mid))["deleted"] is True


async def test_reindex_enfileira(deps):
    out = await _as_curator(handlers.reindex, deps, "a.md", "t")
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

    clients = await _as_curator(handlers.list_agent_clients, deps)
    got = await _as_curator(handlers.get_agent_client, deps, "chatgpt-web")

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

    revealed = await _as_curator(
        handlers.reveal_agent_client_token,
        deps,
        "chatgpt-web",
    )

    assert revealed["slug"] == "chatgpt-web"
    assert revealed["token"] == created["token"]
    assert revealed["token_prefix"] == created["token_prefix"]

    with pytest.raises(PermissionError, match="curator required"):
        await _as_client(
            handlers.reveal_agent_client_token,
            deps,
            "chatgpt-web",
        )


async def test_rotate_agent_client_token_troca_token_e_invalida_hash_antigo(deps):
    created = await _create_client_as_curator(deps, slug="codex", name="Codex")
    async with deps.session_factory() as s:
        before = await repo.get_agent_client(s, slug="codex")
        old_hash = before.token_hash

    rotated = await _as_curator(
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

    out = await _as_curator(
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
            await _as_client(fn, *args, **kwargs)


def _curator_only_call(case: str, deps):
    memory_id = "00000000-0000-0000-0000-000000000000"
    calls = {
        "remember": (handlers.remember, (deps, "trabalho", [{"role": "user", "content": "x"}]), {}),
        "get_memory": (handlers.get_memory, (deps, memory_id), {}),
        "list_memories": (handlers.list_memories, (deps,), {}),
        "update_memory": (handlers.update_memory, (deps, memory_id, "novo"), {}),
        "move_memory": (handlers.move_memory, (deps, memory_id, "trabalho"), {}),
        "delete_memory": (handlers.delete_memory, (deps, memory_id), {}),
        "merge_memories": (handlers.merge_memories, (deps, [memory_id]), {}),
        "get_document": (handlers.get_document, (deps, memory_id), {}),
        "list_documents": (handlers.list_documents, (deps,), {}),
        "reindex": (handlers.reindex, (deps, "a.md", "t"), {}),
        "create_agent_client": (handlers.create_agent_client, (deps, "Codex"), {}),
        "list_agent_clients": (handlers.list_agent_clients, (deps,), {}),
        "get_agent_client": (handlers.get_agent_client, (deps, "chatgpt-web"), {}),
        "reveal_agent_client_token": (
            handlers.reveal_agent_client_token,
            (deps, "chatgpt-web"),
            {},
        ),
        "rotate_agent_client_token": (
            handlers.rotate_agent_client_token,
            (deps, "chatgpt-web"),
            {},
        ),
        "disable_agent_client": (handlers.disable_agent_client, (deps, "chatgpt-web"), {}),
        "get_entity": (handlers.get_entity, (deps, "Pessoa", "t"), {}),
        "search_entities": (handlers.search_entities, (deps, "Pessoa", "t"), {}),
        "get_related": (handlers.get_related, (deps, "Pessoa", "t"), {}),
        "update_entity": (handlers.update_entity, (deps, "Pessoa", "t", {"x": 1}), {}),
        "merge_entities": (handlers.merge_entities, (deps, ["A"], "B", "t"), {}),
        "delete_entity": (handlers.delete_entity, (deps, "Pessoa", "t"), {}),
        "create_namespace": (handlers.create_namespace, (deps, "t", "trabalho"), {}),
        "list_namespaces": (handlers.list_namespaces, (deps,), {}),
    }
    return calls[case]


@pytest.mark.parametrize(
    "case",
    [
        "remember",
        "get_memory",
        "list_memories",
        "update_memory",
        "move_memory",
        "delete_memory",
        "merge_memories",
        "get_document",
        "list_documents",
        "reindex",
        "create_agent_client",
        "list_agent_clients",
        "get_agent_client",
        "reveal_agent_client_token",
        "rotate_agent_client_token",
        "disable_agent_client",
        "get_entity",
        "search_entities",
        "get_related",
        "update_entity",
        "merge_entities",
        "delete_entity",
        "create_namespace",
        "list_namespaces",
    ],
)
async def test_principal_client_nao_acessa_handlers_curator_only_existentes(deps, case):
    fn, args, kwargs = _curator_only_call(case, deps)

    with pytest.raises(PermissionError, match="curator required"):
        await _as_client(fn, *args, **kwargs)


async def test_search_permite_principal_client(deps):
    out = await _as_client(handlers.search, deps, "qualquer coisa")

    assert out == {"results": [], "graph": []}


async def test_client_submit_agent_note_cria_arquivo_nota_e_outbox(deps):
    await _create_client_as_curator(deps)

    out = await _as_client(
        handlers.submit_agent_note,
        deps,
        title="Resumo antes da compressao",
        content="Conteudo livre enviado pelo client.",
        messages=[{"role": "assistant", "content": "Detalhe adicional."}],
        suggested_namespace="brain",
        metadata={"model": "gpt-5.5"},
    )

    assert set(out) == {"note_id", "repo_path", "status", "event_id"}
    assert out["status"] == "pending"
    assert out["repo_path"].startswith("_agents/chatgpt-web/")
    assert out["repo_path"].endswith("-resumo-antes-da-compressao.md")

    note_path = Path(deps.settings.repo_cache_path) / out["repo_path"]
    text = note_path.read_text(encoding="utf-8")
    assert "type: agent_note" in text
    assert f"id: {out['note_id']}" in text
    assert "client_slug: chatgpt-web" in text
    assert "client_name: ChatGPT Web" in text
    assert "Conteudo livre enviado pelo client." in text
    assert "**assistant:** Detalhe adicional." in text

    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(out["note_id"]))
        event = (
            await s.execute(select(OutboxEvent).where(OutboxEvent.id == uuid.UUID(out["event_id"])))
        ).scalar_one()

    assert note.client_slug == "chatgpt-web"
    assert note.repo_path == out["repo_path"]
    assert note.status == "pending"
    assert note.suggested_namespace == "brain"
    assert note.meta == {"model": "gpt-5.5"}
    assert event.type == "agent_note.created"
    assert event.status == "pending"
    assert event.payload["event_type"] == "agent_note.created"
    assert event.payload["type"] == "agent_note.created"
    assert event.payload["agent_note"] == {
        "id": out["note_id"],
        "client_slug": "chatgpt-web",
        "client_name": "ChatGPT Web",
        "repo_path": out["repo_path"],
        "title": "Resumo antes da compressao",
        "suggested_namespace": "brain",
        "metadata": {"model": "gpt-5.5"},
    }
    assert "content" not in event.payload
    assert "messages" not in event.payload


async def test_submit_agent_note_rejeita_curator(deps):
    with pytest.raises(PermissionError, match="client required"):
        await _as_curator(
            handlers.submit_agent_note,
            deps,
            content="Conteudo bruto.",
        )


async def test_submit_agent_note_exige_content_ou_messages(deps):
    await _create_client_as_curator(deps)

    with pytest.raises(ValueError, match="content or messages required"):
        await _as_client(
            handlers.submit_agent_note,
            deps,
            title="Sem corpo",
        )


async def test_submit_agent_note_falha_quando_client_principal_nao_existe(deps):
    principal = auth.Principal("client", "ghost", "Ghost")

    with pytest.raises(ValueError, match="active agent client not found: ghost"):
        await _as_principal(
            principal,
            handlers.submit_agent_note,
            deps,
            content="Conteudo bruto.",
        )


async def test_submit_agent_note_falha_quando_client_esta_inativo(deps):
    await _create_client_as_curator(deps, slug="codex", name="Codex")
    async with deps.session_factory() as s:
        await repo.disable_agent_client(s, "codex")
        await s.commit()

    principal = auth.Principal("client", "codex", "Codex")
    with pytest.raises(ValueError, match="agent client disabled: codex"):
        await _as_principal(
            principal,
            handlers.submit_agent_note,
            deps,
            content="Conteudo bruto.",
        )


async def test_create_agent_client_duplicado_nao_retorna_token_novo_nem_altera_hash(
    deps, monkeypatch
):
    created = await _create_client_as_curator(deps, slug="codex", name="Codex")
    async with deps.session_factory() as s:
        existing = await repo.get_agent_client(s, slug="codex")
        old_hash = existing.token_hash

    original_get = repo.get_agent_client
    calls = 0

    async def racing_get(session, *, slug):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await original_get(session, slug=slug)

    profile_calls = []

    def fail_if_profile_written(*args, **kwargs):
        profile_calls.append((args, kwargs))
        return "_agents/codex/codex.md"

    monkeypatch.setattr(handlers.repo, "get_agent_client", racing_get)
    monkeypatch.setattr(handlers.git_writer, "write_agent_client_profile", fail_if_profile_written)

    with pytest.raises(ValueError, match="agent client already exists"):
        await _create_client_as_curator(deps, slug="codex", name="Codex")

    async with deps.session_factory() as s:
        after = await repo.get_agent_client(s, slug="codex")
    assert after.token_hash == old_hash
    assert profile_calls == []
    assert created["token"].startswith("brain_client_codex_")


async def test_create_agent_client_persiste_credencial_se_profile_git_falha(deps, monkeypatch):
    def fail_profile_write(*args, **kwargs):
        raise RuntimeError("git write failed")

    monkeypatch.setattr(handlers.git_writer, "write_agent_client_profile", fail_profile_write)

    with pytest.raises(RuntimeError, match="git write failed"):
        await _create_client_as_curator(deps, slug="codex", name="Codex")

    async with deps.session_factory() as s:
        client = await repo.get_agent_client(s, slug="codex")
    assert client is not None
    assert client.token_hash


async def test_rotate_agent_client_persiste_credencial_se_profile_git_falha(deps, monkeypatch):
    await _create_client_as_curator(deps, slug="codex", name="Codex")
    async with deps.session_factory() as s:
        before = await repo.get_agent_client(s, slug="codex")
        old_hash = before.token_hash

    def fail_profile_write(*args, **kwargs):
        raise RuntimeError("git write failed")

    monkeypatch.setattr(handlers.git_writer, "write_agent_client_profile", fail_profile_write)

    with pytest.raises(RuntimeError, match="git write failed"):
        await _as_curator(handlers.rotate_agent_client_token, deps, "codex")

    async with deps.session_factory() as s:
        after = await repo.get_agent_client(s, slug="codex")
    assert after.token_hash != old_hash
