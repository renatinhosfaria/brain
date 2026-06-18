import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

import yaml


_AGENT_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}(\d{0,6})?$")


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


def render_markdown(messages: list[dict]) -> str:
    return render_messages_markdown(messages)


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


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


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
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit", "-m", message,
        ],
        dest,
    )
    if push:
        _push_with_retry(dest, retries)


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

    _commit_path(
        dest=dest,
        rel=rel,
        message=f"client: create {safe_client_slug}",
        author_name=author_name,
        author_email=author_email,
        push=push,
        retries=retries,
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
    rel = f"{safe_inbox_dir}/{safe_client_slug}/{yyyy}/{mm}/{dd}/{timestamp}-{file_slug}.md"
    path = _safe_repo_path(dest, rel)
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

    _commit_path(
        dest=dest,
        rel=rel,
        message=f"agent-note: {safe_client_slug} {timestamp}",
        author_name=author_name,
        author_email=author_email,
        push=push,
        retries=retries,
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
) -> str:
    dest = Path(dest)
    rel = validate_curated_note_path(path)
    note_path = _safe_repo_path(dest, rel)
    is_update = note_path.exists()
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        render_frontmatter(frontmatter or {}) + content.rstrip() + "\n",
        encoding="utf-8",
    )

    operation = "update" if is_update else "create"
    _commit_path(
        dest=dest,
        rel=rel,
        message=f"note: {operation} {rel}",
        author_name=author_name,
        author_email=author_email,
        push=push,
        retries=retries,
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
    push: bool = False,
    retries: int = 3,
) -> str:
    """Grava a conversa como .md, faz commit (autor brain-bot) e opcionalmente push. Retorna o repo_path relativo."""
    dest = Path(dest)
    safe_conversations_dir = _validate_relative_path(conversations_dir)
    safe_namespace = _validate_relative_path(namespace)
    first = messages[0]["content"] if messages else "conversa"
    rel = f"{safe_conversations_dir}/{safe_namespace}/{timestamp}-{_slugify(first)}.md"
    path = _safe_repo_path(dest, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(messages), encoding="utf-8")

    _commit_path(
        dest=dest,
        rel=rel,
        message=f"chat: {safe_namespace} {timestamp}",
        author_name=author_name,
        author_email=author_email,
        push=push,
        retries=retries,
    )
    return rel


def _push_with_retry(dest: Path, retries: int) -> None:
    last_error = None
    for _ in range(retries):
        try:
            _git(["push"], dest)
            return
        except subprocess.CalledProcessError as e:  # non-fast-forward etc.
            last_error = e
            _git(["pull", "--rebase"], dest)
    raise RuntimeError(f"push falhou após {retries} tentativas: {last_error}")


def push_repo(dest: str | Path, *, retries: int = 3) -> None:
    _push_with_retry(Path(dest), retries)
