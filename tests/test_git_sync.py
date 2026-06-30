import os
import subprocess
from pathlib import Path

from brain.ingestion import git_sync
from brain.ingestion.git_sync import changed_files, clone_or_pull, content_hash


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)


def test_content_hash_estavel():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_clone_e_changed_files_no_primeiro_clone(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "nota.md").write_text("# oi")
    _git(["add", "."], origin)
    _git(["commit", "-m", "1"], origin)

    dest = tmp_path / "clone"
    before, after = clone_or_pull(str(origin), dest)
    assert before is None
    assert len(after) == 40
    changes = changed_files(dest, None, after)
    assert ("A", "nota.md") in changes


def test_pull_detecta_diff(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "a.md").write_text("# a")
    _git(["add", "."], origin)
    _git(["commit", "-m", "1"], origin)

    dest = tmp_path / "clone"
    _, sha1 = clone_or_pull(str(origin), dest)

    (origin / "b.md").write_text("# b")
    _git(["add", "."], origin)
    _git(["commit", "-m", "2"], origin)

    before, after = clone_or_pull(str(origin), dest)
    assert before == sha1
    changes = changed_files(dest, before, after)
    assert ("A", "b.md") in changes


def test_clone_or_pull_usa_identidade_no_rebase_quando_dest_tem_commits_locais(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    remote = tmp_path / "remote.git"
    other = tmp_path / "other"
    dest = tmp_path / "clone"
    _git(["init", "--bare", "--initial-branch=main", str(remote)], tmp_path)
    _git(["clone", str(remote), str(other)], tmp_path)
    _git(["config", "user.email", "other@example.com"], other)
    _git(["config", "user.name", "other"], other)
    (other / "base.md").write_text("base\n", encoding="utf-8")
    _git(["add", "base.md"], other)
    _git(["commit", "-m", "base"], other)
    _git(["push", "-u", "origin", "main"], other)

    clone_or_pull(str(remote), dest)
    (dest / "note.md").write_text("local\n", encoding="utf-8")
    _git(["add", "note.md"], dest)
    _git(
        [
            "-c",
            "user.name=local",
            "-c",
            "user.email=local@example.com",
            "commit",
            "-m",
            "local note",
        ],
        dest,
    )
    (other / "README.md").write_text("remote\n", encoding="utf-8")
    _git(["add", "README.md"], other)
    _git(["commit", "-m", "remote readme"], other)
    _git(["push"], other)
    assert (
        subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=dest,
            capture_output=True,
            text=True,
        ).returncode
        == 1
    )

    clone_or_pull(
        str(remote),
        dest,
        committer_name="brain-bot",
        committer_email="brain-bot@example.com",
    )

    assert (
        subprocess.run(
            ["git", "status", "--short"],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    assert (
        subprocess.run(
            ["git", "show", "HEAD:README.md"],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "remote\n"
    )
    assert (
        subprocess.run(
            ["git", "show", "HEAD:note.md"],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "local\n"
    )


def test_rename_markdown_emite_delete_e_add(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "a.md").write_text("# a")
    _git(["add", "."], origin)
    _git(["commit", "-m", "1"], origin)

    dest = tmp_path / "clone"
    _, _ = clone_or_pull(str(origin), dest)

    _git(["mv", "a.md", "b.md"], origin)
    _git(["commit", "-m", "rename"], origin)

    before, after = clone_or_pull(str(origin), dest)
    changes = changed_files(dest, before, after)

    assert ("D", "a.md") in changes
    assert ("A", "b.md") in changes


def test_clone_com_token_nao_coloca_token_na_url_do_remote(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, cwd=None, token=None):
        calls.append((args, cwd, token))
        if args[0] == "clone":
            (tmp_path / "clone" / ".git").mkdir(parents=True)
            return ""
        if args[:2] == ["rev-parse", "HEAD"]:
            return "a" * 40 + "\n"
        raise AssertionError(args)

    monkeypatch.setattr(git_sync, "_run", fake_run)

    clone_or_pull("https://github.com/acme/vault.git", tmp_path / "clone", token="ghp_secret")

    clone_args, _, clone_token = calls[0]
    assert clone_args == ["clone", "https://github.com/acme/vault.git", str(tmp_path / "clone")]
    assert clone_token == "ghp_secret"
    assert "ghp_secret" not in " ".join(clone_args)
