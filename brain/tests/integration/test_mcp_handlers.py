import asyncio
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
from brain.storage.models import AgentNote, Base, Chunk, OutboxEvent
from brain.storage import repositories as repo


CURATOR = auth.Principal("curator", "hermes", "Hermes")
CLIENT = auth.Principal("client", "chatgpt-web", "ChatGPT Web")


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.2] * 2000 for _ in texts]


class FailingEmbedder:
    async def embed(self, texts):
        raise RuntimeError("embed failed")


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


async def _submit_note_as_client(deps, principal=CLIENT, **kwargs):
    return await _as_principal(
        principal,
        handlers.submit_agent_note,
        deps,
        **({"title": "Nota bruta", "content": "Conteudo bruto."} | kwargs),
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


async def test_create_note_grava_arquivo_cria_pais_indexa_e_retorna_id_path(deps):
    out = await _as_curator(
        handlers.create_note,
        deps,
        "projetos/brain.md",
        "# Brain\n\nConteudo curado.",
        metadata={"owner": "Hermes"},
        source_agent_note_ids=["agent-note-1"],
    )

    assert out["path"] == "projetos/brain.md"
    assert out["repo_path"] == "projetos/brain.md"
    assert out["id"]

    note_path = Path(deps.settings.repo_cache_path) / "projetos" / "brain.md"
    assert note_path.exists()
    text = note_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "type: curated_note" in text
    assert "# Brain\n\nConteudo curado." in text

    async with deps.session_factory() as s:
        doc = await repo.get_document(s, repo_path="projetos/brain.md")
        chunks = list((await s.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars())

    assert str(doc.id) == out["id"]
    assert doc.namespace == "curated"
    assert doc.title == "Brain"
    assert doc.meta["metadata"] == {"owner": "Hermes"}
    assert doc.meta["source_agent_note_ids"] == ["agent-note-1"]
    assert len(chunks) >= 1


async def test_create_note_rejeita_agents(deps):
    with pytest.raises(ValueError, match="_agents"):
        await _as_curator(
            handlers.create_note,
            deps,
            "_agents/chatgpt-web/raw.md",
            "# Raw\n\nNao pode.",
        )


async def test_create_note_falha_quando_path_existe(deps):
    await _as_curator(handlers.create_note, deps, "projetos/brain.md", "# Brain\n\nPrimeiro.")

    with pytest.raises(ValueError, match="already exists|ja existe|existe"):
        await _as_curator(handlers.create_note, deps, "projetos/brain.md", "# Brain\n\nSegundo.")


async def test_create_note_concorrente_mesmo_path_so_uma_sucede(deps, monkeypatch):
    original_get_document = repo.get_document
    both_prechecked = asyncio.Event()
    precheck_calls = 0

    async def gated_get_document(session, *, id=None, repo_path=None):
        nonlocal precheck_calls
        if id is None and repo_path == "projetos/race.md" and precheck_calls < 2:
            precheck_calls += 1
            if precheck_calls == 2:
                both_prechecked.set()
            await both_prechecked.wait()
            return None
        return await original_get_document(session, id=id, repo_path=repo_path)

    monkeypatch.setattr(repo, "get_document", gated_get_document)

    results = await asyncio.gather(
        _as_curator(
            handlers.create_note,
            deps,
            "projetos/race.md",
            "# Race\n\nConteudo vencedor A.",
        ),
        _as_curator(
            handlers.create_note,
            deps,
            "projetos/race.md",
            "# Race\n\nConteudo vencedor B.",
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, ValueError)]
    text = (Path(deps.settings.repo_cache_path) / "projetos/race.md").read_text(encoding="utf-8")

    assert len(successes) == 1
    assert len(failures) == 1
    assert text.count("Conteudo vencedor") == 1
    assert successes[0]["content"] == text
    async with deps.session_factory() as s:
        doc = await original_get_document(s, repo_path="projetos/race.md")
    assert doc.raw_content == text


async def test_create_note_recupera_arquivo_existente_sem_documento_apos_falha_indexacao(
    deps, monkeypatch
):
    deps.settings.git_push_enabled = True
    push_calls = []
    monkeypatch.setattr(handlers.git_writer, "push_repo", lambda *args, **kwargs: push_calls.append(args))
    monkeypatch.setattr(
        handlers.git_writer,
        "_push_with_retry",
        lambda *args, **kwargs: push_calls.append(args),
    )

    deps.embedder = FailingEmbedder()
    with pytest.raises(RuntimeError, match="embed failed"):
        await _as_curator(
            handlers.create_note,
            deps,
            "projetos/recovery.md",
            "# Recovery\n\nConteudo gravado antes da falha.",
            metadata={"attempt": 1},
        )

    note_path = Path(deps.settings.repo_cache_path) / "projetos/recovery.md"
    assert note_path.exists()
    written_text = note_path.read_text(encoding="utf-8")
    assert push_calls == []
    async with deps.session_factory() as s:
        assert await repo.get_document(s, repo_path="projetos/recovery.md") is None

    deps.embedder = FakeEmbedder()
    recovered = await _as_curator(
        handlers.create_note,
        deps,
        "projetos/recovery.md",
        "# Recovery\n\nConteudo de retry nao deve sobrescrever.",
        metadata={"attempt": 1},
    )

    assert recovered["path"] == "projetos/recovery.md"
    assert recovered["content"] == written_text
    assert "Conteudo de retry" not in note_path.read_text(encoding="utf-8")
    async with deps.session_factory() as s:
        doc = await repo.get_document(s, repo_path="projetos/recovery.md")
    assert doc.raw_content == written_text


async def test_create_note_recuperacao_usa_frontmatter_existente_para_metadata(deps):
    deps.embedder = FailingEmbedder()
    with pytest.raises(RuntimeError, match="embed failed"):
        await _as_curator(
            handlers.create_note,
            deps,
            "projetos/frontmatter.md",
            "# Frontmatter\n\nConteudo original.",
            metadata={"owner": "original", "attempt": 1},
            source_agent_note_ids=["agent-original"],
        )

    note_path = Path(deps.settings.repo_cache_path) / "projetos/frontmatter.md"
    written_text = note_path.read_text(encoding="utf-8")
    assert "owner: original" in written_text
    async with deps.session_factory() as s:
        assert await repo.get_document(s, repo_path="projetos/frontmatter.md") is None

    deps.embedder = FakeEmbedder()
    recovered = await _as_curator(
        handlers.create_note,
        deps,
        "projetos/frontmatter.md",
        "# Frontmatter\n\nConteudo de retry.",
        metadata={"owner": "retry", "attempt": 2},
        source_agent_note_ids=["agent-retry"],
    )
    got = await _as_client(handlers.get_note, deps, recovered["id"])

    assert recovered["content"] == written_text
    assert recovered["metadata"] == {"owner": "original", "attempt": 1}
    assert recovered["source_agent_note_ids"] == ["agent-original"]
    assert got["metadata"] == {"owner": "original", "attempt": 1}
    assert got["source_agent_note_ids"] == ["agent-original"]


async def test_create_note_commit_falha_restaura_worktree_e_retry_nao_indexa_uncommitted(
    deps, monkeypatch
):
    deps.settings.git_push_enabled = True
    push_calls = []
    monkeypatch.setattr(handlers.git_writer, "push_repo", lambda *args, **kwargs: push_calls.append(args))
    original_commit_path = handlers.git_writer._commit_path
    commit_calls = 0

    def fail_once_after_stage(*, dest, rel, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            handlers.git_writer._git(["add", "--", rel], dest)
            raise RuntimeError("commit failed")
        return original_commit_path(dest=dest, rel=rel, **kwargs)

    monkeypatch.setattr(handlers.git_writer, "_commit_path", fail_once_after_stage)

    with pytest.raises(RuntimeError, match="commit failed"):
        await _as_curator(
            handlers.create_note,
            deps,
            "projetos/uncommitted.md",
            "# Uncommitted\n\nPrimeiro corpo nao comitado.",
        )

    note_path = Path(deps.settings.repo_cache_path) / "projetos/uncommitted.md"
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=deps.settings.repo_cache_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert not note_path.exists()
    assert status == ""
    assert push_calls == []
    async with deps.session_factory() as s:
        assert await repo.get_document(s, repo_path="projetos/uncommitted.md") is None

    retried = await _as_curator(
        handlers.create_note,
        deps,
        "projetos/uncommitted.md",
        "# Uncommitted\n\nCorpo de retry comitado.",
    )

    assert commit_calls == 2
    assert push_calls
    assert "Corpo de retry comitado." in retried["content"]
    assert "Primeiro corpo nao comitado." not in retried["content"]
    async with deps.session_factory() as s:
        doc = await repo.get_document(s, repo_path="projetos/uncommitted.md")
    assert doc.raw_content == retried["content"]


async def test_update_note_substitui_markdown_inteiro_e_reindexa(deps):
    created = await _as_curator(handlers.create_note, deps, "projetos/brain.md", "# Brain\n\nAntigo.")

    updated = await _as_curator(
        handlers.update_note,
        deps,
        created["id"],
        "# Brain Atualizado\n\nNovo conteudo.",
        metadata={"reviewed": True},
    )

    assert updated["id"] == created["id"]
    assert updated["path"] == "projetos/brain.md"
    text = (Path(deps.settings.repo_cache_path) / "projetos/brain.md").read_text(encoding="utf-8")
    assert "# Brain Atualizado\n\nNovo conteudo." in text
    assert "Antigo" not in text

    async with deps.session_factory() as s:
        doc = await repo.get_document(s, repo_path="projetos/brain.md")
        chunks = list((await s.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars())

    assert str(doc.id) == created["id"]
    assert doc.title == "Brain Atualizado"
    assert doc.raw_content == text
    assert doc.meta["metadata"] == {"reviewed": True}
    assert any("Novo conteudo." in chunk.text for chunk in chunks)
    assert all("Antigo" not in chunk.text for chunk in chunks)


async def test_update_note_falha_indexacao_mantem_get_note_db_e_retry_reconcilia(
    deps, monkeypatch
):
    created = await _as_curator(handlers.create_note, deps, "projetos/brain.md", "# Brain\n\nAntigo.")
    deps.settings.git_push_enabled = True
    push_calls = []
    monkeypatch.setattr(handlers.git_writer, "push_repo", lambda *args, **kwargs: push_calls.append(args))
    monkeypatch.setattr(
        handlers.git_writer,
        "_push_with_retry",
        lambda *args, **kwargs: push_calls.append(args),
    )

    deps.embedder = FailingEmbedder()
    with pytest.raises(RuntimeError, match="embed failed"):
        await _as_curator(
            handlers.update_note,
            deps,
            created["id"],
            "# Brain\n\nNovo conteudo falhou.",
            metadata={"attempt": 1},
        )

    note_path = Path(deps.settings.repo_cache_path) / "projetos/brain.md"
    file_text = note_path.read_text(encoding="utf-8")
    stale = await _as_client(handlers.get_note, deps, created["id"])

    assert "Novo conteudo falhou." in file_text
    assert "Antigo." in stale["content"]
    assert "Novo conteudo falhou." not in stale["content"]
    assert push_calls == []

    deps.embedder = FakeEmbedder()
    updated = await _as_curator(
        handlers.update_note,
        deps,
        created["id"],
        "# Brain\n\nNovo conteudo falhou.",
        metadata={"attempt": 1},
    )

    assert updated["id"] == created["id"]
    assert updated["content"] == file_text
    async with deps.session_factory() as s:
        doc = await repo.get_document(s, repo_path="projetos/brain.md")
    assert doc.raw_content == file_text
    assert doc.meta["metadata"] == {"attempt": 1}


async def test_get_note_retorna_apenas_curated_notes(deps):
    created = await _as_curator(handlers.create_note, deps, "projetos/brain.md", "# Brain\n\nCurado.")
    async with deps.session_factory() as s:
        raw = await repo.upsert_document(
            s,
            namespace="_agents",
            repo_path="_agents/chatgpt-web/raw.md",
            title="Raw",
            raw_content="# Raw\n\nBruto.",
            content_hash="raw",
            commit_sha=None,
        )
        raw_id = str(raw.id)
        await s.commit()

    got_by_id = await _as_client(handlers.get_note, deps, created["id"])
    got_by_path = await _as_client(handlers.get_note, deps, "projetos/brain.md")
    raw_by_id = await _as_client(handlers.get_note, deps, raw_id)

    assert got_by_id["id"] == created["id"]
    assert got_by_id["path"] == "projetos/brain.md"
    assert got_by_id["content"].startswith("---\n")
    assert "# Brain\n\nCurado." in got_by_id["content"]
    assert got_by_path["id"] == created["id"]
    assert raw_by_id is None


async def test_get_note_agents_path_eh_forbidden_ou_not_found(deps):
    with pytest.raises(ValueError, match="_agents"):
        await _as_client(handlers.get_note, deps, "_agents/chatgpt-web/raw.md")


async def test_list_vault_tree_lista_dirs_e_notas_excluindo_agents_por_padrao(deps):
    await _as_curator(handlers.create_note, deps, "projetos/brain.md", "# Brain\n\nCurado.")
    await _as_curator(handlers.create_note, deps, "areas/trabalho.md", "# Trabalho\n\nCurado.")
    agents_dir = Path(deps.settings.repo_cache_path) / "_agents" / "chatgpt-web"
    agents_dir.mkdir(parents=True)
    (agents_dir / "raw.md").write_text("# Raw\n\nBruto.", encoding="utf-8")

    tree = await _as_curator(handlers.list_vault_tree, deps)
    entries = {(item["type"], item["path"]) for item in tree["items"]}

    assert ("directory", "projetos") in entries
    assert ("note", "projetos/brain.md") in entries
    assert ("directory", "areas") in entries
    assert ("note", "areas/trabalho.md") in entries
    assert all(not item["path"].startswith("_agents") for item in tree["items"])

    agents_tree = await _as_curator(handlers.list_vault_tree, deps, include_agents=True)
    assert ("directory", "_agents") in {
        (item["type"], item["path"]) for item in agents_tree["items"]
    }


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
        "remember": ("remember", (deps, "trabalho", [{"role": "user", "content": "x"}]), {}),
        "get_memory": ("get_memory", (deps, memory_id), {}),
        "list_memories": ("list_memories", (deps,), {}),
        "update_memory": ("update_memory", (deps, memory_id, "novo"), {}),
        "move_memory": ("move_memory", (deps, memory_id, "trabalho"), {}),
        "delete_memory": ("delete_memory", (deps, memory_id), {}),
        "merge_memories": ("merge_memories", (deps, [memory_id]), {}),
        "get_document": ("get_document", (deps, memory_id), {}),
        "list_documents": ("list_documents", (deps,), {}),
        "reindex": ("reindex", (deps, "a.md", "t"), {}),
        "create_note": ("create_note", (deps, "projetos/brain.md", "# Brain"), {}),
        "update_note": ("update_note", (deps, memory_id, "# Brain"), {}),
        "list_vault_tree": ("list_vault_tree", (deps,), {}),
        "create_agent_client": ("create_agent_client", (deps, "Codex"), {}),
        "list_agent_clients": ("list_agent_clients", (deps,), {}),
        "get_agent_client": ("get_agent_client", (deps, "chatgpt-web"), {}),
        "reveal_agent_client_token": (
            "reveal_agent_client_token",
            (deps, "chatgpt-web"),
            {},
        ),
        "rotate_agent_client_token": (
            "rotate_agent_client_token",
            (deps, "chatgpt-web"),
            {},
        ),
        "disable_agent_client": ("disable_agent_client", (deps, "chatgpt-web"), {}),
        "list_agent_notes": ("list_agent_notes", (deps,), {}),
        "get_agent_note": ("get_agent_note", (deps, memory_id), {}),
        "claim_agent_note": ("claim_agent_note", (deps, memory_id), {}),
        "complete_agent_note": ("complete_agent_note", (deps, memory_id), {}),
        "reject_agent_note": ("reject_agent_note", (deps, memory_id), {}),
        "fail_agent_note": ("fail_agent_note", (deps, memory_id), {}),
        "get_entity": ("get_entity", (deps, "Pessoa", "t"), {}),
        "search_entities": ("search_entities", (deps, "Pessoa", "t"), {}),
        "get_related": ("get_related", (deps, "Pessoa", "t"), {}),
        "update_entity": ("update_entity", (deps, "Pessoa", "t", {"x": 1}), {}),
        "merge_entities": ("merge_entities", (deps, ["A"], "B", "t"), {}),
        "delete_entity": ("delete_entity", (deps, "Pessoa", "t"), {}),
        "create_namespace": ("create_namespace", (deps, "t", "trabalho"), {}),
        "list_namespaces": ("list_namespaces", (deps,), {}),
    }
    handler_name, args, kwargs = calls[case]
    return getattr(handlers, handler_name), args, kwargs


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
        "create_note",
        "update_note",
        "list_vault_tree",
        "create_agent_client",
        "list_agent_clients",
        "get_agent_client",
        "reveal_agent_client_token",
        "rotate_agent_client_token",
        "disable_agent_client",
        "list_agent_notes",
        "get_agent_note",
        "claim_agent_note",
        "complete_agent_note",
        "reject_agent_note",
        "fail_agent_note",
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

    out = await _submit_note_as_client(
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
    assert "-resumo-antes-da-compressao-" in out["repo_path"]
    assert out["note_id"] in out["repo_path"]
    assert out["repo_path"].endswith(".md")

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
        "repo_path": out["repo_path"],
    }
    assert "content" not in event.payload
    assert "messages" not in event.payload
    assert "client_name" not in event.payload["agent_note"]
    assert "title" not in event.payload["agent_note"]
    assert "suggested_namespace" not in event.payload["agent_note"]
    assert "metadata" not in event.payload["agent_note"]


async def test_curator_lista_agent_notes_pendentes_sem_conteudo_bruto(deps):
    await _create_client_as_curator(deps)
    await _create_client_as_curator(deps, slug="codex", name="Codex")
    codex = auth.Principal("client", "codex", "Codex")
    first = await _submit_note_as_client(
        deps,
        title="Primeira nota",
        content="Conteudo que nao deve aparecer na listagem.",
        metadata={"source": "chatgpt"},
    )
    second = await _submit_note_as_client(deps, title="Segunda nota", content="Outro conteudo.")
    await _submit_note_as_client(deps, codex, title="Nota Codex", content="Conteudo Codex.")

    page = await _as_curator(
        handlers.list_agent_notes,
        deps,
        status="pending",
        client_slug="chatgpt-web",
        limit=1,
    )

    assert set(page) == {"items", "next_cursor"}
    assert len(page["items"]) == 1
    assert page["next_cursor"] is not None
    assert page["items"][0]["client_slug"] == "chatgpt-web"
    assert page["items"][0]["status"] == "pending"
    assert "content" not in page["items"][0]

    next_page = await _as_curator(
        handlers.list_agent_notes,
        deps,
        status="pending",
        client_slug="chatgpt-web",
        limit=1,
        cursor=page["next_cursor"],
    )

    listed_ids = {page["items"][0]["id"], next_page["items"][0]["id"]}
    assert listed_ids == {first["note_id"], second["note_id"]}
    assert next_page["next_cursor"] is None


async def test_list_agent_notes_cursor_nao_duplica_quando_nota_nova_entrar(deps):
    await _create_client_as_curator(deps)
    originals = [
        await _submit_note_as_client(deps, title=f"Nota original {i}", content=f"Conteudo {i}.")
        for i in range(3)
    ]
    original_page = await _as_curator(handlers.list_agent_notes, deps, status="pending", limit=10)
    original_ids = [item["id"] for item in original_page["items"]]
    assert set(original_ids) == {note["note_id"] for note in originals}

    first_page = await _as_curator(handlers.list_agent_notes, deps, status="pending", limit=2)
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"] is not None

    await _submit_note_as_client(deps, title="Nota mais nova", content="Entrou entre paginas.")

    second_page = await _as_curator(
        handlers.list_agent_notes,
        deps,
        status="pending",
        limit=2,
        cursor=first_page["next_cursor"],
    )

    listed_ids = [item["id"] for item in first_page["items"] + second_page["items"]]
    assert listed_ids[: len(original_ids)] == original_ids
    assert len(set(listed_ids)) == len(listed_ids)


async def test_curator_obtem_conteudo_bruto_da_agent_note(deps):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(
        deps,
        title="Nota com markdown",
        content="Conteudo livre enviado pelo client.",
        messages=[{"role": "assistant", "content": "Detalhe adicional."}],
        suggested_namespace="brain",
        metadata={"model": "gpt-5.5"},
    )

    got = await _as_curator(handlers.get_agent_note, deps, submitted["note_id"])

    assert got["id"] == submitted["note_id"]
    assert got["repo_path"] == submitted["repo_path"]
    assert got["status"] == "pending"
    assert got["metadata"] == {"model": "gpt-5.5"}
    assert got["suggested_namespace"] == "brain"
    assert "Conteudo livre enviado pelo client." in got["content"]
    assert "**assistant:** Detalhe adicional." in got["content"]


async def test_client_nao_lista_nem_obtem_agent_notes_brutas(deps):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)

    with pytest.raises(PermissionError, match="curator required"):
        await _as_client(handlers.list_agent_notes, deps)
    with pytest.raises(PermissionError, match="curator required"):
        await _as_client(handlers.get_agent_note, deps, submitted["note_id"])


async def test_claim_agent_note_muda_pending_para_in_review(deps):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)

    claimed = await _as_curator(handlers.claim_agent_note, deps, submitted["note_id"])

    assert claimed["status"] == "in_review"
    assert claimed["claimed_at"] is not None
    assert claimed["completed_at"] is None
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(submitted["note_id"]))
    assert note.status == "in_review"
    assert note.claimed_at is not None


@pytest.mark.parametrize("claim_first", [False, True])
async def test_complete_agent_note_funciona_de_pending_ou_in_review(deps, claim_first):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)
    if claim_first:
        await _as_curator(handlers.claim_agent_note, deps, submitted["note_id"])

    completed = await _as_curator(handlers.complete_agent_note, deps, submitted["note_id"])

    assert completed["status"] == "curated"
    assert completed["completed_at"] is not None
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(submitted["note_id"]))
    assert note.status == "curated"
    assert note.completed_at is not None


@pytest.mark.parametrize("claim_first", [False, True])
async def test_reject_agent_note_funciona_de_pending_ou_in_review(deps, claim_first):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)
    if claim_first:
        await _as_curator(handlers.claim_agent_note, deps, submitted["note_id"])

    rejected = await _as_curator(
        handlers.reject_agent_note,
        deps,
        submitted["note_id"],
        reason="Sem informacao persistente.",
    )

    assert rejected["status"] == "rejected"
    assert rejected["outcome"]["reason"] == "Sem informacao persistente."
    assert rejected["completed_at"] is not None
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(submitted["note_id"]))
    assert note.status == "rejected"
    assert note.outcome == {"reason": "Sem informacao persistente."}


async def test_fail_agent_note_armazena_error(deps):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)

    failed = await _as_curator(
        handlers.fail_agent_note,
        deps,
        submitted["note_id"],
        error="Falha ao processar markdown.",
    )

    assert failed["status"] == "failed"
    assert failed["error"] == "Falha ao processar markdown."
    assert failed["completed_at"] is not None
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(submitted["note_id"]))
    assert note.status == "failed"
    assert note.error == "Falha ao processar markdown."


async def test_complete_agent_note_armazena_outcome_flexivel(deps):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)
    outcome = {
        "created": [{"namespace": "brain", "memory_id": str(uuid.uuid4())}],
        "skipped": False,
        "notes": ["mantem estrutura flexivel"],
    }

    completed = await _as_curator(
        handlers.complete_agent_note,
        deps,
        submitted["note_id"],
        outcome=outcome,
    )

    assert completed["status"] == "curated"
    assert completed["outcome"] == outcome
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(submitted["note_id"]))
    assert note.outcome == outcome


async def test_agent_note_terminal_nao_volta_para_estado_nao_terminal(deps):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)
    await _as_curator(handlers.complete_agent_note, deps, submitted["note_id"])

    with pytest.raises(ValueError, match="terminal"):
        await _as_curator(handlers.claim_agent_note, deps, submitted["note_id"])

    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(submitted["note_id"]))
    assert note.status == "curated"


async def test_agent_note_transicoes_terminais_concorrentes_so_uma_vence(deps, monkeypatch):
    await _create_client_as_curator(deps)
    submitted = await _submit_note_as_client(deps)
    note_uuid = uuid.UUID(submitted["note_id"])
    original_get_agent_note = repo.get_agent_note
    both_read_pending = asyncio.Event()
    reads_before_transition = 0

    async def gated_get_agent_note(session, id):
        nonlocal reads_before_transition
        note = await original_get_agent_note(session, id)
        if id == note_uuid and note is not None and note.status == "pending":
            reads_before_transition += 1
            if reads_before_transition == 2:
                both_read_pending.set()
            if reads_before_transition <= 2:
                await both_read_pending.wait()
        return note

    monkeypatch.setattr(repo, "get_agent_note", gated_get_agent_note)

    results = await asyncio.gather(
        _as_curator(handlers.complete_agent_note, deps, submitted["note_id"], {"winner": "complete"}),
        _as_curator(handlers.reject_agent_note, deps, submitted["note_id"], "concorrente"),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, ValueError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0]["status"] in {"curated", "rejected"}
    async with deps.session_factory() as s:
        note = await original_get_agent_note(s, note_uuid)
    assert note.status == successes[0]["status"]
    assert note.completed_at is not None


async def test_submit_agent_note_mesmo_timestamp_e_titulo_cria_paths_distintos(
    deps, monkeypatch
):
    await _create_client_as_curator(deps)
    monkeypatch.setattr(handlers, "_now_stamp", lambda: "20260617T183000000000")

    first = await _submit_note_as_client(
        deps,
        title="Resumo repetido",
        content="Primeira nota.",
    )
    second = await _submit_note_as_client(
        deps,
        title="Resumo repetido",
        content="Segunda nota.",
    )

    assert first["repo_path"] != second["repo_path"]
    assert first["note_id"] in first["repo_path"]
    assert second["note_id"] in second["repo_path"]
    assert (Path(deps.settings.repo_cache_path) / first["repo_path"]).exists()
    assert (Path(deps.settings.repo_cache_path) / second["repo_path"]).exists()

    async with deps.session_factory() as s:
        notes = list((await s.execute(select(AgentNote))).scalars().all())
        events = list((await s.execute(select(OutboxEvent))).scalars().all())

    assert {note.repo_path for note in notes} == {first["repo_path"], second["repo_path"]}
    assert {event.payload["agent_note"]["repo_path"] for event in events} == {
        first["repo_path"],
        second["repo_path"],
    }


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


async def test_submit_agent_note_rejeita_client_sem_permissao_sem_escrever(deps):
    async with deps.session_factory() as s:
        await repo.create_agent_client(
            s,
            slug="search-only",
            name="Search Only",
            description=None,
            token_prefix="brain_client_search-only",
            token_hash="hash-search-only",
            token_encrypted="encrypted-search-only",
            permissions=["search"],
            meta=None,
        )
        await s.commit()

    principal = auth.Principal("client", "search-only", "Search Only")
    with pytest.raises(PermissionError, match="submit_agent_note permission required"):
        await _as_principal(
            principal,
            handlers.submit_agent_note,
            deps,
            title="Nao pode",
            content="Nao deve escrever.",
        )

    async with deps.session_factory() as s:
        assert list((await s.execute(select(AgentNote))).scalars().all()) == []
        assert list((await s.execute(select(OutboxEvent))).scalars().all()) == []
    assert not (Path(deps.settings.repo_cache_path) / "_agents" / "search-only").exists()


async def test_submit_agent_note_persiste_db_e_outbox_quando_push_falha(deps, monkeypatch):
    await _create_client_as_curator(deps)
    deps.settings.git_push_enabled = True

    def fail_push(*args, **kwargs):
        raise RuntimeError("push failed after local commit")

    monkeypatch.setattr(handlers.git_writer, "push_repo", fail_push, raising=False)
    monkeypatch.setattr(handlers.git_writer, "_push_with_retry", fail_push)

    with pytest.raises(RuntimeError, match="push failed after local commit"):
        await _submit_note_as_client(
            deps,
            title="Push posterior",
            content="Conteudo que deve continuar rastreavel.",
        )

    async with deps.session_factory() as s:
        notes = list((await s.execute(select(AgentNote))).scalars().all())
        events = list((await s.execute(select(OutboxEvent))).scalars().all())

    assert len(notes) == 1
    assert notes[0].status == "pending"
    assert notes[0].client_slug == "chatgpt-web"
    assert notes[0].repo_path.startswith("_agents/chatgpt-web/")
    assert len(events) == 1
    assert events[0].type == "agent_note.created"
    assert events[0].payload["agent_note"]["id"] == str(notes[0].id)
    assert events[0].payload["agent_note"]["repo_path"] == notes[0].repo_path


async def test_submit_agent_note_rollback_db_quando_git_write_falha(deps, monkeypatch):
    await _create_client_as_curator(deps)

    def fail_write(*args, **kwargs):
        raise RuntimeError("git write failed")

    monkeypatch.setattr(handlers.git_writer, "write_agent_note", fail_write)

    with pytest.raises(RuntimeError, match="git write failed"):
        await _submit_note_as_client(
            deps,
            title="Falha antes do commit git",
            content="Nao deve deixar nota pendente.",
        )

    async with deps.session_factory() as s:
        assert list((await s.execute(select(AgentNote))).scalars().all()) == []
        assert list((await s.execute(select(OutboxEvent))).scalars().all()) == []


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
