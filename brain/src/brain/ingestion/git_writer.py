import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

import yaml


def slugify(text: str, *, fallback: str = "note") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-+", "-", text)[:50].strip("-")
    return slug or fallback


def _slugify(text: str) -> str:
    return slugify(text, fallback="conversa")


def validate_curated_note_path(path: str) -> str:
    raw = str(path).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:/", raw):
        raise ValueError("path deve ser relativo")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path nao pode conter '..'")
        parts.append(part)

    if not parts:
        raise ValueError("path vazio")
    if parts[0] == "_agents":
        raise ValueError("notas curadas nao podem ser gravadas em _agents/")

    rel = "/".join(parts)
    if not rel.endswith(".md"):
        raise ValueError("notas curadas devem usar extensao .md")
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
            if key_text != "token_prefix" and ("token" in key_text or "secret" in key_text):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_token_values(item, token)
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
    safe_metadata = _redact_token_values(metadata or {}, token)
    frontmatter = render_frontmatter(
        {
            "type": "agent_client",
            "client_slug": safe_slug,
            "client_name": client_name,
            "token_prefix": token_prefix,
            "metadata": safe_metadata or None,
        }
    )

    sections = [frontmatter, f"# {client_name}\n"]
    sections.append(f"Client slug: `{safe_slug}`\n")
    sections.append(f"Token prefix: `{token_prefix}`\n")

    if description:
        sections.append(f"\n## Description\n\n{description}\n")
    if capture_policy:
        sections.append(f"\n## Capture Policy\n\n{capture_policy}\n")
    if recommended_instructions:
        sections.append(f"\n## Recommended Instructions\n\n{recommended_instructions}\n")
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
    _git(["add", rel], dest)
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
    safe_client_slug = slugify(client_slug, fallback="client")
    rel = f"{inbox_dir}/{safe_client_slug}/{safe_client_slug}.md"
    path = dest / rel
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
    if not re.match(r"^\d{8}T", timestamp):
        raise ValueError("timestamp deve iniciar com yyyymmddT")

    dest = Path(dest)
    safe_client_slug = slugify(client_slug, fallback="client")
    yyyy, mm, dd = timestamp[:4], timestamp[4:6], timestamp[6:8]
    title_source = title or content or note_id
    file_slug = slugify(title_source, fallback="note")
    rel = f"{inbox_dir}/{safe_client_slug}/{yyyy}/{mm}/{dd}/{timestamp}-{file_slug}.md"
    path = dest / rel
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
    note_path = dest / rel
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
    first = messages[0]["content"] if messages else "conversa"
    rel = f"{conversations_dir}/{namespace}/{timestamp}-{_slugify(first)}.md"
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(messages), encoding="utf-8")

    _git(["add", rel], dest)
    _git(
        [
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit", "-m", f"chat: {namespace} {timestamp}",
        ],
        dest,
    )
    if push:
        _push_with_retry(dest, retries)
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
