import re
from pathlib import Path

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_repo_path(
    repo_cache_path: str | Path,
    repo_path: str,
    *,
    require_markdown: bool,
) -> tuple[str, Path]:
    if not isinstance(repo_path, str):
        raise ValueError("repo_path must be a string")

    raw = repo_path.replace("\\", "/")
    if raw.startswith(":"):
        raise ValueError("repo_path cannot use pathspec magic")
    if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise ValueError("repo_path must be relative")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("repo_path cannot contain '..'")
        parts.append(part)

    if not parts:
        raise ValueError("repo_path is empty")

    rel = "/".join(parts)
    if rel == "_agents" or rel.startswith("_agents/"):
        raise ValueError("agent notes are not indexed as curated documents")
    if require_markdown and not rel.endswith(".md"):
        raise ValueError("repo_path must end with .md")

    try:
        repo_root = Path(repo_cache_path).resolve()
        resolved = (repo_root / rel).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("repo_path cannot be resolved") from exc

    try:
        resolved_rel = resolved.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError("repo_path escapes repository") from exc

    if resolved_rel == "_agents" or resolved_rel.startswith("_agents/"):
        raise ValueError("agent notes are not indexed as curated documents")
    if require_markdown and not resolved_rel.endswith(".md"):
        raise ValueError("repo_path must end with .md")

    return rel, resolved
