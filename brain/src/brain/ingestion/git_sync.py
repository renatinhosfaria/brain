import hashlib
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: str | Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _auth_url(url: str, token: str | None) -> str:
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://{token}@", 1)
    return url


def head_sha(dest: str | Path) -> str:
    return _run(["rev-parse", "HEAD"], cwd=dest).strip()


def clone_or_pull(repo_url: str, dest: str | Path, token: str | None = None) -> tuple[str | None, str]:
    """Retorna (sha_antes, sha_depois). sha_antes é None no primeiro clone."""
    dest = Path(dest)
    if (dest / ".git").exists():
        before = head_sha(dest)
        _run(["pull", "--rebase"], cwd=dest)
        return before, head_sha(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["clone", _auth_url(repo_url, token), str(dest)])
    return None, head_sha(dest)


def changed_files(dest: str | Path, old_sha: str | None, new_sha: str) -> list[tuple[str, str]]:
    """Lista (status, path) de arquivos .md alterados. status: A/M/D."""
    if old_sha is None:
        out = _run(["ls-files", "*.md"], cwd=dest)
        return [("A", p) for p in out.splitlines() if p]
    out = _run(["diff", "--name-status", old_sha, new_sha], cwd=dest)
    changes = []
    for line in out.splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if path.endswith(".md"):
            changes.append((status[0], path))
    return changes
