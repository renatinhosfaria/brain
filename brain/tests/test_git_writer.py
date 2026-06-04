import subprocess
from pathlib import Path

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


def test_render_markdown():
    md = render_markdown([{"role": "user", "content": "oi"}])
    assert "**user:** oi" in md


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
