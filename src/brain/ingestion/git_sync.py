import base64
import hashlib
import os
import subprocess
from pathlib import Path


def _git_env(
    token: str | None = None,
    *,
    committer_name: str | None = None,
    committer_email: str | None = None,
) -> dict[str, str] | None:
    if not token and not committer_name and not committer_email:
        return None
    env = os.environ.copy()
    if token:
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
            }
        )
    if committer_name:
        env["GIT_COMMITTER_NAME"] = committer_name
    if committer_email:
        env["GIT_COMMITTER_EMAIL"] = committer_email
    return env


def _run(
    args: list[str],
    cwd: str | Path | None = None,
    token: str | None = None,
    *,
    committer_name: str | None = None,
    committer_email: str | None = None,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(
            token,
            committer_name=committer_name,
            committer_email=committer_email,
        ),
    )
    return result.stdout


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def head_sha(dest: str | Path) -> str:
    return _run(["rev-parse", "HEAD"], cwd=dest).strip()


def _head_sha_or_none(dest: str | Path) -> str | None:
    try:
        return head_sha(dest)
    except subprocess.CalledProcessError:
        return None


def clone_or_pull(
    repo_url: str,
    dest: str | Path,
    token: str | None = None,
    *,
    committer_name: str | None = None,
    committer_email: str | None = None,
) -> tuple[str | None, str]:
    """Retorna (sha_antes, sha_depois). sha_antes é None no primeiro clone."""
    dest = Path(dest)
    if (dest / ".git").exists():
        before = _head_sha_or_none(dest)
        if before is None:
            return None, ""
        _run(
            ["pull", "--rebase"],
            cwd=dest,
            token=token,
            committer_name=committer_name,
            committer_email=committer_email,
        )
        return before, head_sha(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["clone", repo_url, str(dest)], token=token)
    return None, _head_sha_or_none(dest) or ""


def changed_files(dest: str | Path, old_sha: str | None, new_sha: str) -> list[tuple[str, str]]:
    """Lista (status, path) de arquivos .md alterados. status: A/M/D."""
    if old_sha is None:
        out = _run(["ls-files", "*.md"], cwd=dest)
        return [("A", p) for p in out.splitlines() if p]
    out = _run(["diff", "--name-status", old_sha, new_sha], cwd=dest)
    changes = []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") and len(parts) == 3:
            old_path, new_path = parts[1], parts[2]
            if old_path.endswith(".md"):
                changes.append(("D", old_path))
            if new_path.endswith(".md"):
                changes.append(("A", new_path))
            continue
        path = parts[-1]
        if path.endswith(".md"):
            changes.append((status[0], path))
    return changes
