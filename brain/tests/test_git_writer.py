import subprocess
from pathlib import Path

import pytest
from brain.ingestion import git_writer
from brain.ingestion.git_writer import render_markdown, write_conversation, _slugify


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)


def test_slugify():
    assert _slugify("Olá, Mundo!") == "ola-mundo"
    assert _slugify("") == "conversa"


def test_slugify_publico_gera_slug_seguro_para_paths():
    slug = git_writer.slugify(" ChatGPT Web/../Prod! ")
    assert slug == "chatgpt-web-prod"
    assert "/" not in slug
    assert ".." not in slug
    assert git_writer.slugify("!!!", fallback="client") == "client"


def test_render_markdown():
    md = render_markdown([{"role": "user", "content": "oi"}])
    assert "**user:** oi" in md


def test_render_messages_markdown_usa_markdown_simples():
    md = git_writer.render_messages_markdown([
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "ola"},
    ])
    assert md == "**user:** oi\n\n**assistant:** ola\n"


def test_render_frontmatter_omite_campos_none():
    md = git_writer.render_frontmatter({
        "type": "curated_note",
        "title": "Brain",
        "empty": None,
        "metadata": {"source": "test"},
    })

    assert md.startswith("---\n")
    assert "type: curated_note" in md
    assert "title: Brain" in md
    assert "source: test" in md
    assert "empty" not in md


def test_render_agent_client_profile_nao_inclui_token_completo():
    full_token = "brain_client_chatgpt-web_super-secret-token"

    md = git_writer.render_agent_client_profile(
        client_slug="chatgpt-web",
        client_name="ChatGPT Web",
        token_prefix="brain_client_chatgpt-web",
        token=full_token,
        description="Cliente usado pelo ChatGPT.",
        capture_policy="Capture somente fatos persistentes.",
        recommended_instructions="Use search antes de submeter notas.",
        metadata={"owner": "Hermes"},
    )

    assert "brain_client_chatgpt-web" in md
    assert full_token not in md
    assert "super-secret-token" not in md
    assert "Cliente usado pelo ChatGPT." in md
    assert "Capture somente fatos persistentes." in md
    assert "Use search antes de submeter notas." in md
    assert "owner: Hermes" in md


def test_render_agent_client_profile_redige_token_em_campos_textuais():
    full_token = "brain_client_chatgpt-web_super-secret-token"

    md = git_writer.render_agent_client_profile(
        client_slug="chatgpt-web",
        client_name="ChatGPT Web",
        token_prefix="brain_client_chatgpt-web",
        token=full_token,
        description=f"Use {full_token} para autenticar.",
        capture_policy=f"Nunca grave {full_token}.",
        recommended_instructions=f"Configure bearer {full_token}.",
        metadata={"notes": [f"token completo: {full_token}"]},
    )

    assert full_token not in md
    assert "super-secret-token" not in md
    assert "Use [redacted] para autenticar." in md
    assert "Nunca grave [redacted]." in md
    assert "Configure bearer [redacted]." in md
    assert "token completo: [redacted]" in md


def test_render_agent_client_profile_redige_token_em_chaves_de_metadata():
    full_token = "brain_client_chatgpt-web_super-secret-token"

    md = git_writer.render_agent_client_profile(
        client_slug="chatgpt-web",
        client_name="ChatGPT Web",
        token_prefix="brain_client_chatgpt-web",
        token=full_token,
        metadata={full_token: "value", "nested": {f"key-{full_token}": "value"}},
    )

    assert full_token not in md
    assert "super-secret-token" not in md
    assert "'[redacted]': '[redacted]'" in md
    assert "key-[redacted]: '[redacted]'" in md


def test_write_agent_client_profile_cria_perfil_sem_token_completo(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    full_token = "brain_client_chatgpt-web_super-secret-token"

    rel = git_writer.write_agent_client_profile(
        repo,
        inbox_dir="_agents",
        client_slug="chatgpt-web",
        client_name="ChatGPT Web",
        token_prefix="brain_client_chatgpt-web",
        token=full_token,
        description="Cliente usado pelo ChatGPT.",
        capture_policy="Capture somente fatos persistentes.",
        recommended_instructions="Use search antes de submeter notas.",
        metadata={"owner": "Hermes"},
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
    )

    assert rel == "_agents/chatgpt-web/chatgpt-web.md"
    text = (repo / rel).read_text(encoding="utf-8")
    assert full_token not in text
    assert "super-secret-token" not in text
    assert "token_prefix: brain_client_chatgpt-web" in text
    subject = _git(["log", "-1", "--format=%s"], repo).stdout.strip()
    assert subject == "client: create chatgpt-web"


def test_write_agent_client_profile_rejeita_inbox_dir_inseguro_sem_escrever_fora(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    outside = tmp_path / "outside"

    with pytest.raises(ValueError):
        git_writer.write_agent_client_profile(
            repo,
            inbox_dir="../outside",
            client_slug="chatgpt-web",
            client_name="ChatGPT Web",
            token_prefix="brain_client_chatgpt-web",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
        )

    assert not outside.exists()


def test_write_agent_client_profile_rollback_create_quando_commit_falha(tmp_path, monkeypatch):
    repo = tmp_path / "vault"
    _init_repo(repo)

    def fail_after_stage(*, dest, rel, **kwargs):
        git_writer._git(["add", "--", rel], dest)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(git_writer, "_commit_path", fail_after_stage)

    with pytest.raises(RuntimeError, match="commit failed"):
        git_writer.write_agent_client_profile(
            repo,
            inbox_dir="_agents",
            client_slug="chatgpt-web",
            client_name="ChatGPT Web",
            token_prefix="brain_client_chatgpt-web",
            token="brain_client_chatgpt-web_secret",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
        )

    assert not (repo / "_agents" / "chatgpt-web" / "chatgpt-web.md").exists()
    assert not (repo / "_agents").exists()
    assert _git(["status", "--short"], repo).stdout == ""


def test_write_agent_client_profile_mantem_commit_local_quando_push_falha(
    tmp_path, monkeypatch
):
    repo = tmp_path / "vault"
    _init_repo(repo)

    def fail_push(*args, **kwargs):
        raise RuntimeError("push failed after local commit")

    monkeypatch.setattr(git_writer, "_push_with_retry", fail_push)

    rel = "_agents/chatgpt-web/chatgpt-web.md"
    with pytest.raises(RuntimeError, match="push failed after local commit"):
        git_writer.write_agent_client_profile(
            repo,
            inbox_dir="_agents",
            client_slug="chatgpt-web",
            client_name="ChatGPT Web",
            token_prefix="brain_client_chatgpt-web",
            token="brain_client_chatgpt-web_secret",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=True,
        )

    assert (repo / rel).exists()
    assert "token_prefix: brain_client_chatgpt-web" in (repo / rel).read_text(
        encoding="utf-8"
    )
    committed = _git(["show", f"HEAD:{rel}"], repo).stdout
    assert "token_prefix: brain_client_chatgpt-web" in committed
    assert _git(["status", "--short"], repo).stdout == ""


def test_write_agent_note_cria_apenas_na_pasta_do_client(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)

    rel = git_writer.write_agent_note(
        repo,
        inbox_dir="_agents",
        client_slug="chatgpt-web",
        client_name="ChatGPT Web",
        note_id="agent_note_1",
        title="Resumo",
        content="Conteudo livre",
        messages=[{"role": "user", "content": "oi"}],
        suggested_namespace="brain",
        metadata={"model": "gpt"},
        timestamp="20260617T183000000000",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
    )

    assert rel == "_agents/chatgpt-web/2026/06/17/20260617T183000000000-resumo-agent-note-1.md"
    text = (repo / rel).read_text(encoding="utf-8")
    assert "type: agent_note" in text
    assert "id: agent_note_1" in text
    assert "client_slug: chatgpt-web" in text
    assert "client_name: ChatGPT Web" in text
    assert "timestamp: 20260617T183000000000" in text
    assert "suggested_namespace: brain" in text
    assert "model: gpt" in text
    assert "Conteudo livre" in text
    assert "**user:** oi" in text
    subject = _git(["log", "-1", "--format=%s"], repo).stdout.strip()
    assert subject == "agent-note: chatgpt-web 20260617T183000000000"


def test_write_agent_note_mesmo_timestamp_e_titulo_cria_paths_distintos(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    common = {
        "inbox_dir": "_agents",
        "client_slug": "chatgpt-web",
        "client_name": "ChatGPT Web",
        "title": "Resumo",
        "content": "Conteudo livre",
        "timestamp": "20260617T183000000000",
        "author_name": "brain-bot",
        "author_email": "brain-bot@example.com",
        "push": False,
    }

    first = git_writer.write_agent_note(repo, note_id="agent_note_1", **common)
    second = git_writer.write_agent_note(repo, note_id="agent_note_2", **common)

    assert first != second
    assert first.endswith("20260617T183000000000-resumo-agent-note-1.md")
    assert second.endswith("20260617T183000000000-resumo-agent-note-2.md")
    assert (repo / first).read_text(encoding="utf-8") != (repo / second).read_text(encoding="utf-8")
    assert "id: agent_note_1" in (repo / first).read_text(encoding="utf-8")
    assert "id: agent_note_2" in (repo / second).read_text(encoding="utf-8")


def test_write_agent_note_rejeita_timestamp_inseguro_sem_escrever_fora(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    outside = tmp_path / "outside-resumo.md"

    with pytest.raises(ValueError):
        git_writer.write_agent_note(
            repo,
            inbox_dir="_agents",
            client_slug="chatgpt-web",
            client_name="ChatGPT Web",
            note_id="agent_note_1",
            title="Resumo",
            content="Conteudo livre",
            timestamp="20260617T183000/../../../../../../outside",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
        )

    assert not outside.exists()


def test_write_agent_note_rollback_create_quando_commit_falha(tmp_path, monkeypatch):
    repo = tmp_path / "vault"
    _init_repo(repo)

    def fail_after_stage(*, dest, rel, **kwargs):
        git_writer._git(["add", "--", rel], dest)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(git_writer, "_commit_path", fail_after_stage)

    with pytest.raises(RuntimeError, match="commit failed"):
        git_writer.write_agent_note(
            repo,
            inbox_dir="_agents",
            client_slug="chatgpt-web",
            client_name="ChatGPT Web",
            note_id="agent_note_1",
            title="Resumo",
            content="Nao deve sobrar.",
            timestamp="20260617T183000000000",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
        )

    assert not (repo / "_agents" / "chatgpt-web").exists()
    assert not (repo / "_agents").exists()
    assert _git(["status", "--short"], repo).stdout == ""


def test_write_agent_note_mantem_commit_local_quando_push_falha(tmp_path, monkeypatch):
    repo = tmp_path / "vault"
    _init_repo(repo)

    def fail_push(*args, **kwargs):
        raise RuntimeError("push failed after local commit")

    monkeypatch.setattr(git_writer, "_push_with_retry", fail_push)

    rel = (
        "_agents/chatgpt-web/2026/06/17/"
        "20260617T183000000000-resumo-agent-note-1.md"
    )
    with pytest.raises(RuntimeError, match="push failed after local commit"):
        git_writer.write_agent_note(
            repo,
            inbox_dir="_agents",
            client_slug="chatgpt-web",
            client_name="ChatGPT Web",
            note_id="agent_note_1",
            title="Resumo",
            content="Conteudo que deve ficar commitado.",
            timestamp="20260617T183000000000",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=True,
        )

    assert (repo / rel).exists()
    assert "Conteudo que deve ficar commitado." in (repo / rel).read_text(
        encoding="utf-8"
    )
    committed = _git(["show", f"HEAD:{rel}"], repo).stdout
    assert "Conteudo que deve ficar commitado." in committed
    assert _git(["status", "--short"], repo).stdout == ""


@pytest.mark.parametrize(
    "path",
    [
        "_agents/raw.md",
        "./_agents/raw.md",
        "/tmp/raw.md",
        "C:/tmp/raw.md",
        "projetos/../raw.md",
        "projetos/raw.txt",
        ":(glob)*.md",
    ],
)
def test_validate_curated_note_path_rejeita_paths_invalidos(path):
    with pytest.raises(ValueError):
        git_writer.validate_curated_note_path(path)


def test_validate_curated_note_path_normaliza_prefixo_local():
    assert git_writer.validate_curated_note_path("./projetos/brain.md") == "projetos/brain.md"


def test_write_curated_note_cria_pais_automaticamente_e_commita_create_update(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)

    rel = git_writer.write_curated_note(
        repo,
        "./projetos/brain/resumo.md",
        frontmatter={"type": "curated_note", "title": "Resumo"},
        content="# Resumo\n\nConteudo curado.",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
    )

    assert rel == "projetos/brain/resumo.md"
    path = repo / rel
    assert path.exists()
    assert path.parent.is_dir()
    text = path.read_text(encoding="utf-8")
    assert "type: curated_note" in text
    assert "Conteudo curado." in text
    subject = _git(["log", "-1", "--format=%s"], repo).stdout.strip()
    assert subject == "note: create projetos/brain/resumo.md"

    git_writer.write_curated_note(
        repo,
        rel,
        frontmatter={"type": "curated_note", "title": "Resumo"},
        content="# Resumo\n\nConteudo atualizado.",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
    )

    subject = _git(["log", "-1", "--format=%s"], repo).stdout.strip()
    assert subject == "note: update projetos/brain/resumo.md"


def test_write_curated_note_expected_exists_controla_create_update(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)

    git_writer.write_curated_note(
        repo,
        "projetos/brain.md",
        frontmatter={"type": "curated_note"},
        content="# Brain\n\nPrimeiro.",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
        expected_exists=False,
    )

    with pytest.raises(ValueError, match="already exists"):
        git_writer.write_curated_note(
            repo,
            "projetos/brain.md",
            frontmatter={"type": "curated_note"},
            content="# Brain\n\nSegundo.",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
            expected_exists=False,
        )

    with pytest.raises(ValueError, match="does not exist"):
        git_writer.write_curated_note(
            repo,
            "projetos/inexistente.md",
            frontmatter={"type": "curated_note"},
            content="# Inexistente",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
            expected_exists=True,
        )

    git_writer.write_curated_note(
        repo,
        "projetos/brain.md",
        frontmatter={"type": "curated_note"},
        content="# Brain\n\nAtualizado.",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
        expected_exists=True,
    )

    assert "Atualizado." in (repo / "projetos/brain.md").read_text(encoding="utf-8")


def test_write_curated_note_rollback_create_quando_commit_falha(tmp_path, monkeypatch):
    repo = tmp_path / "vault"
    _init_repo(repo)

    def fail_after_stage(*, dest, rel, **kwargs):
        git_writer._git(["add", "--", rel], dest)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(git_writer, "_commit_path", fail_after_stage)

    with pytest.raises(RuntimeError, match="commit failed"):
        git_writer.write_curated_note(
            repo,
            "projetos/brain.md",
            frontmatter={"type": "curated_note"},
            content="# Brain\n\nNao deve sobrar.",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
            expected_exists=False,
        )

    assert not (repo / "projetos/brain.md").exists()
    assert not (repo / "projetos").exists()
    assert _git(["status", "--short"], repo).stdout == ""


def test_write_curated_note_rollback_update_quando_commit_falha(tmp_path, monkeypatch):
    repo = tmp_path / "vault"
    _init_repo(repo)
    rel = git_writer.write_curated_note(
        repo,
        "projetos/brain.md",
        frontmatter={"type": "curated_note"},
        content="# Brain\n\nOriginal.",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
        expected_exists=False,
    )
    original_text = (repo / rel).read_text(encoding="utf-8")

    def fail_after_stage(*, dest, rel, **kwargs):
        git_writer._git(["add", "--", rel], dest)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(git_writer, "_commit_path", fail_after_stage)

    with pytest.raises(RuntimeError, match="commit failed"):
        git_writer.write_curated_note(
            repo,
            rel,
            frontmatter={"type": "curated_note"},
            content="# Brain\n\nAtualizado sem commit.",
            author_name="brain-bot",
            author_email="brain-bot@example.com",
            push=False,
            expected_exists=True,
        )

    assert (repo / rel).read_text(encoding="utf-8") == original_text
    assert _git(["status", "--short"], repo).stdout == ""


def test_write_conversation_cria_arquivo_e_commit(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    rel = write_conversation(
        repo, "conversas", "trabalho",
        [{"role": "user", "content": "preciso lembrar disso"}],
        timestamp="20260604T120000",
        author_name="brain-bot",
        author_email="brain-bot@x",
        push=False,
    )
    assert rel.startswith("conversas/trabalho/20260604T120000-")
    assert (repo / rel).exists()
    # o último commit é do brain-bot
    author = _git(["log", "-1", "--format=%an"], repo).stdout.strip()
    assert author == "brain-bot"


def test_write_conversation_rejeita_namespace_inseguro_sem_escrever_fora(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    outside = tmp_path / "outside"

    with pytest.raises(ValueError):
        write_conversation(
            repo,
            "conversas",
            "../../outside",
            [{"role": "user", "content": "segredo"}],
            timestamp="20260604T120000",
            author_name="brain-bot",
            author_email="brain-bot@x",
            push=False,
        )

    assert not outside.exists()
