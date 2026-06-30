import json
import re
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from brain.ingestion.git_sync import _git_env

_AGENT_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}(\d{0,6})?$")
_CURATED_NOTE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_CURATED_NOTE_LOCKS_GUARD = threading.Lock()


def slugify(text: str, *, fallback: str = "note") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-+", "-", text)[:50].strip("-")
    return slug or fallback


def _slugify(text: str) -> str:
    return slugify(text, fallback="conversa")


def _validate_relative_path(path: str, *, required_suffix: str | None = None) -> str:
    raw = str(path).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:/", raw):
        raise ValueError("path deve ser relativo")
    if raw.startswith(":"):
        raise ValueError("path nao pode usar pathspec magic")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path nao pode conter '..'")
        parts.append(part)

    if not parts:
        raise ValueError("path vazio")

    rel = "/".join(parts)
    if required_suffix and not rel.endswith(required_suffix):
        raise ValueError(f"path deve terminar com {required_suffix}")
    return rel


def _safe_repo_path(dest: Path, rel: str) -> Path:
    repo_root = dest.resolve()
    path = (repo_root / rel).resolve()
    if path != repo_root and repo_root not in path.parents:
        raise ValueError("path deve ficar dentro do repositorio")
    return path


def _validate_agent_timestamp(timestamp: str) -> str:
    if not _AGENT_TIMESTAMP_RE.fullmatch(timestamp):
        raise ValueError("timestamp deve usar formato yyyymmddThhmmss[ffffff]")
    return timestamp


def validate_curated_note_path(path: str) -> str:
    rel = _validate_relative_path(path, required_suffix=".md")
    parts = rel.split("/")
    if parts[0] == "_agents":
        raise ValueError("notas curadas nao podem ser gravadas em _agents/")
    return rel


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


def render_frontmatter(data: dict) -> str:
    cleaned = _drop_none(data)
    body = yaml.safe_dump(
        cleaned,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return f"---\n{body}---\n\n"


def render_messages_markdown(messages: list[dict]) -> str:
    if not messages:
        return ""
    return "\n\n".join(f"**{m['role']}:** {m['content']}" for m in messages) + "\n"


def render_markdown(
    messages: list[dict],
    *,
    timestamp: str | None = None,
    metadata: dict | None = None,
) -> str:
    header = []
    if timestamp is not None or metadata:
        header.append("---")
        if timestamp is not None:
            header.append(f'timestamp: "{timestamp}"')
        if metadata:
            header.append("metadata: " + json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        header.append("---")
        header.append("")
    return "\n".join([*header, render_messages_markdown(messages)])


def render_curated_note(*, frontmatter: dict | None = None, content: str) -> str:
    return render_frontmatter(frontmatter or {}) + content.rstrip() + "\n"


def parse_frontmatter(markdown: str) -> dict:
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---", 4)
    if end == -1:
        return {}
    data = yaml.safe_load(markdown[4:end]) or {}
    return data if isinstance(data, dict) else {}


def _redact_token_values(value: Any, token: str | None) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            safe_key = _redact_token_values(key, token)
            if key_text != "token_prefix" and ("token" in key_text or "secret" in key_text):
                redacted[safe_key] = "[redacted]"
            else:
                redacted[safe_key] = _redact_token_values(item, token)
        return redacted
    if isinstance(value, list):
        return [_redact_token_values(item, token) for item in value]
    if token and isinstance(value, str) and token in value:
        return value.replace(token, "[redacted]")
    return value


def render_agent_client_profile(
    *,
    client_slug: str,
    client_name: str,
    token_prefix: str,
    token: str | None = None,
    description: str | None = None,
    capture_policy: str | None = None,
    recommended_instructions: str | None = None,
    metadata: dict | None = None,
) -> str:
    safe_slug = slugify(client_slug, fallback="client")
    safe_client_name = _redact_token_values(client_name, token)
    safe_description = _redact_token_values(description, token)
    safe_capture_policy = _redact_token_values(capture_policy, token)
    safe_recommended_instructions = _redact_token_values(recommended_instructions, token)
    safe_metadata = _redact_token_values(metadata or {}, token)
    frontmatter = render_frontmatter(
        {
            "type": "agent_client",
            "client_slug": safe_slug,
            "client_name": safe_client_name,
            "token_prefix": token_prefix,
            "metadata": safe_metadata or None,
        }
    )

    sections = [frontmatter, f"# {safe_client_name}\n"]
    sections.append(f"Client slug: `{safe_slug}`\n")
    sections.append(f"Token prefix: `{token_prefix}`\n")

    if safe_description:
        sections.append(f"\n## Description\n\n{safe_description}\n")
    if safe_capture_policy:
        sections.append(f"\n## Capture Policy\n\n{safe_capture_policy}\n")
    if safe_recommended_instructions:
        sections.append(f"\n## Recommended Instructions\n\n{safe_recommended_instructions}\n")
    if safe_metadata:
        metadata_yaml = yaml.safe_dump(
            safe_metadata,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()
        sections.append(f"\n## Metadata\n\n```yaml\n{metadata_yaml}\n```\n")

    return "".join(sections)


def render_agent_note(
    *,
    client_slug: str,
    client_name: str,
    note_id: str,
    timestamp: str,
    title: str | None = None,
    content: str | None = None,
    messages: list[dict] | None = None,
    suggested_namespace: str | None = None,
    metadata: dict | None = None,
) -> str:
    safe_slug = slugify(client_slug, fallback="client")
    frontmatter = render_frontmatter(
        {
            "type": "agent_note",
            "id": note_id,
            "client_slug": safe_slug,
            "client_name": client_name,
            "title": title,
            "created_at": timestamp,
            "timestamp": timestamp,
            "suggested_namespace": suggested_namespace,
            "metadata": metadata,
        }
    )

    body_parts: list[str] = []
    if content:
        body_parts.append(content.rstrip())
    if messages:
        body_parts.append(render_messages_markdown(messages).rstrip())
    body = "\n\n".join(body_parts)
    return frontmatter + body + ("\n" if body else "")


def _git(
    args: list[str],
    cwd: Path,
    token: str | None = None,
    *,
    committer_name: str | None = None,
    committer_email: str | None = None,
) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(
            token,
            committer_name=committer_name,
            committer_email=committer_email,
        ),
    )


def _git_stdout(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _unstage_path(dest: Path, rel: str) -> None:
    result = subprocess.run(
        ["git", "restore", "--staged", "--", rel],
        cwd=dest,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    subprocess.run(
        ["git", "rm", "--cached", "--ignore-unmatch", "--", rel],
        cwd=dest,
        check=False,
        capture_output=True,
        text=True,
    )


def _remove_empty_parent_dirs(dest: Path, note_path: Path) -> None:
    root = dest.resolve()
    current = note_path.parent.resolve()
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _rollback_curated_note_write(
    *,
    dest: Path,
    rel: str,
    note_path: Path,
    existed: bool,
    previous_content: str | None,
) -> None:
    _unstage_path(dest, rel)
    if existed:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(previous_content or "", encoding="utf-8")
    elif note_path.exists():
        note_path.unlink()
        _remove_empty_parent_dirs(dest, note_path)


def _commit_path(
    *,
    dest: Path,
    rel: str,
    message: str,
    author_name: str,
    author_email: str,
    push: bool,
    retries: int,
) -> None:
    _git(["add", "--", rel], dest)
    _git(
        [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            message,
        ],
        dest,
    )
    if push:
        _push_with_retry(
            dest,
            retries,
            author_name=author_name,
            author_email=author_email,
        )


def write_agent_client_profile(
    dest: str | Path,
    *,
    inbox_dir: str = "_agents",
    client_slug: str,
    client_name: str,
    token_prefix: str,
    token: str | None = None,
    description: str | None = None,
    capture_policy: str | None = None,
    recommended_instructions: str | None = None,
    metadata: dict | None = None,
    author_name: str,
    author_email: str,
    push: bool = False,
    retries: int = 3,
) -> str:
    dest = Path(dest)
    safe_inbox_dir = _validate_relative_path(inbox_dir)
    safe_client_slug = slugify(client_slug, fallback="client")
    rel = f"{safe_inbox_dir}/{safe_client_slug}/{safe_client_slug}.md"
    path = _safe_repo_path(dest, rel)
    existed = path.exists()
    previous_content = path.read_text(encoding="utf-8") if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_agent_client_profile(
            client_slug=safe_client_slug,
            client_name=client_name,
            token_prefix=token_prefix,
            token=token,
            description=description,
            capture_policy=capture_policy,
            recommended_instructions=recommended_instructions,
            metadata=metadata,
        ),
        encoding="utf-8",
    )

    try:
        _commit_path(
            dest=dest,
            rel=rel,
            message=f"client: create {safe_client_slug}",
            author_name=author_name,
            author_email=author_email,
            push=False,
            retries=retries,
        )
    except Exception:
        _rollback_curated_note_write(
            dest=dest,
            rel=rel,
            note_path=path,
            existed=existed,
            previous_content=previous_content,
        )
        raise
    if push:
        _push_with_retry(
            dest,
            retries,
            author_name=author_name,
            author_email=author_email,
        )
    return rel


def write_agent_note(
    dest: str | Path,
    *,
    inbox_dir: str = "_agents",
    client_slug: str,
    client_name: str,
    note_id: str,
    timestamp: str,
    title: str | None = None,
    content: str | None = None,
    messages: list[dict] | None = None,
    suggested_namespace: str | None = None,
    metadata: dict | None = None,
    author_name: str,
    author_email: str,
    push: bool = False,
    retries: int = 3,
) -> str:
    dest = Path(dest)
    timestamp = _validate_agent_timestamp(timestamp)
    safe_inbox_dir = _validate_relative_path(inbox_dir)
    safe_client_slug = slugify(client_slug, fallback="client")
    yyyy, mm, dd = timestamp[:4], timestamp[4:6], timestamp[6:8]
    title_source = title or content or note_id
    file_slug = slugify(title_source, fallback="note")
    note_slug = slugify(note_id, fallback="note-id")
    rel = (
        f"{safe_inbox_dir}/{safe_client_slug}/{yyyy}/{mm}/{dd}/"
        f"{timestamp}-{file_slug}-{note_slug}.md"
    )
    path = _safe_repo_path(dest, rel)
    existed = path.exists()
    previous_content = path.read_text(encoding="utf-8") if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_agent_note(
            client_slug=safe_client_slug,
            client_name=client_name,
            note_id=note_id,
            timestamp=timestamp,
            title=title,
            content=content,
            messages=messages,
            suggested_namespace=suggested_namespace,
            metadata=metadata,
        ),
        encoding="utf-8",
    )

    try:
        _commit_path(
            dest=dest,
            rel=rel,
            message=f"agent-note: {safe_client_slug} {timestamp}",
            author_name=author_name,
            author_email=author_email,
            push=False,
            retries=retries,
        )
    except Exception:
        _rollback_curated_note_write(
            dest=dest,
            rel=rel,
            note_path=path,
            existed=existed,
            previous_content=previous_content,
        )
        raise
    if push:
        _push_with_retry(
            dest,
            retries,
            author_name=author_name,
            author_email=author_email,
        )
    return rel


def write_curated_note(
    dest: str | Path,
    path: str,
    *,
    frontmatter: dict | None = None,
    content: str,
    author_name: str,
    author_email: str,
    push: bool = False,
    retries: int = 3,
    expected_exists: bool | None = None,
) -> str:
    dest = Path(dest)
    rel = validate_curated_note_path(path)
    note_path = _safe_repo_path(dest, rel)
    lock_key = (str(dest.resolve()), rel)
    with _CURATED_NOTE_LOCKS_GUARD:
        lock = _CURATED_NOTE_LOCKS.setdefault(lock_key, threading.Lock())

    with lock:
        exists = note_path.exists()
        if expected_exists is False and exists:
            raise ValueError(f"curated note already exists: {rel}")
        if expected_exists is True and not exists:
            raise ValueError(f"curated note does not exist: {rel}")

        previous_content = note_path.read_text(encoding="utf-8") if exists else None
        rendered = render_curated_note(frontmatter=frontmatter, content=content)
        if exists and previous_content == rendered:
            return rel

        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(rendered, encoding="utf-8")

        operation = "update" if exists else "create"
        try:
            _commit_path(
                dest=dest,
                rel=rel,
                message=f"note: {operation} {rel}",
                author_name=author_name,
                author_email=author_email,
                push=False,
                retries=retries,
            )
        except Exception:
            _rollback_curated_note_write(
                dest=dest,
                rel=rel,
                note_path=note_path,
                existed=exists,
                previous_content=previous_content,
            )
            raise
        if push:
            _push_with_retry(
                dest,
                retries,
                author_name=author_name,
                author_email=author_email,
            )
    return rel


def write_conversation(
    dest: str | Path,
    conversations_dir: str,
    namespace: str,
    messages: list[dict],
    *,
    timestamp: str,
    author_name: str,
    author_email: str,
    metadata: dict | None = None,
    push: bool = False,
    token: str | None = None,
    retries: int = 3,
) -> str:
    """Grava a conversa como .md, faz commit (autor brain-bot) e opcionalmente push.

    Retorna o repo_path relativo.
    """
    dest = Path(dest)
    safe_conversations_dir = _validate_relative_path(conversations_dir)
    safe_namespace = _validate_relative_path(namespace)
    first = messages[0]["content"] if messages else "conversa"
    rel = f"{safe_conversations_dir}/{safe_namespace}/{timestamp}-{_slugify(first)}.md"
    path = _safe_repo_path(dest, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_markdown(messages, timestamp=timestamp, metadata=metadata),
        encoding="utf-8",
    )

    _commit_path(
        dest=dest,
        rel=rel,
        message=f"chat: {safe_namespace} {timestamp}",
        author_name=author_name,
        author_email=author_email,
        push=False,
        retries=retries,
    )
    if push:
        _push_with_retry(
            dest,
            retries,
            token=token,
            author_name=author_name,
            author_email=author_email,
        )
    return rel


def _push_with_retry(
    dest: Path,
    retries: int,
    token: str | None = None,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> None:
    last_error = None
    for _ in range(retries):
        try:
            _git(["push"], dest, token=token)
            return
        except subprocess.CalledProcessError as e:  # non-fast-forward etc.
            last_error = e
            local_head = _git_stdout(["rev-parse", "HEAD"], dest).strip()
            was_clean = _git_stdout(["status", "--short"], dest) == ""
            try:
                _git(
                    ["pull", "--rebase"],
                    dest,
                    token=token,
                    committer_name=author_name,
                    committer_email=author_email,
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=dest,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=dest,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if was_clean:
                    _git(["reset", "--hard", local_head], dest)
                raise
    raise RuntimeError(f"push falhou após {retries} tentativas: {last_error}")


def push_repo(
    dest: str | Path,
    *,
    retries: int = 3,
    token: str | None = None,
    author_name: str | None = None,
    author_email: str | None = None,
) -> None:
    _push_with_retry(
        Path(dest),
        retries,
        token=token,
        author_name=author_name,
        author_email=author_email,
    )
