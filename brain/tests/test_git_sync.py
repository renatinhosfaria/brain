import subprocess
from pathlib import Path

from brain.ingestion.git_sync import changed_files, clone_or_pull, content_hash, head_sha


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
