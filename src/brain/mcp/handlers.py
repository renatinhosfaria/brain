import base64
import binascii
import datetime as dt
import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from brain import auth
from brain.graph import age
from brain.ingestion import git_writer
from brain.ingestion import pipeline
from brain.notes.links import extract_obsidian_links
from brain.queue.base import JobType
from brain.repo_paths import normalize_repo_path
from brain.search.retriever import deep_search as _deep_search
from brain.search.retriever import search as _search
from brain.storage import repositories as repo


@dataclass
class Deps:
    session_factory: object
    embedder: object
    llm: object
    queue: object
    settings: object


def _now_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")


def _require_curator():
    p = auth.get_current_principal()
    if p.type != "curator":
        raise PermissionError("curator required")
    return p


def _require_client():
    p = auth.get_current_principal()
    if p.type != "client":
        raise PermissionError("client required")
    return p


def _require_client_or_curator():
    return auth.get_current_principal()


def _require_deep_search_principal():
    p = auth.get_current_principal()
    if p.type not in {"client", "curator"}:
        raise PermissionError("client or curator required")
    return p


def _bounded_int(value, *, name: str, min_value: int, max_value: int) -> int:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} deve ser um inteiro entre {min_value} e {max_value}")
    if value < min_value or value > max_value:
        raise ValueError(f"{name} deve ser entre {min_value} e {max_value}")
    return value


def _require_token_encryption_key(settings) -> str:
    key = settings.brain_token_encryption_key
    if not key:
        raise RuntimeError("brain_token_encryption_key required")
    return key


def _token_prefix(token: str, slug: str) -> str:
    base_len = len(f"brain_client_{slug}_")
    prefix_len = min(len(token) - 1, base_len + 8)
    return token[:prefix_len]


def _stored_client_meta(
    *,
    metadata: dict | None,
    capture_policy: str | None,
    recommended_instructions: str | None,
) -> dict:
    return {
        "metadata": metadata or {},
        "capture_policy": capture_policy,
        "recommended_instructions": recommended_instructions,
    }


def _profile_fields(client) -> dict:
    meta = client.meta or {}
    if any(k in meta for k in ("metadata", "capture_policy", "recommended_instructions")):
        return {
            "metadata": meta.get("metadata") or {},
            "capture_policy": meta.get("capture_policy"),
            "recommended_instructions": meta.get("recommended_instructions"),
        }
    return {
        "metadata": meta,
        "capture_policy": None,
        "recommended_instructions": None,
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _agent_client_dict(client) -> dict | None:
    if client is None:
        return None
    fields = _profile_fields(client)
    return {
        "id": str(client.id),
        "slug": client.slug,
        "name": client.name,
        "description": client.description,
        "status": client.status,
        "token_prefix": client.token_prefix,
        "permissions": client.permissions or [],
        "metadata": fields["metadata"],
        "capture_policy": fields["capture_policy"],
        "recommended_instructions": fields["recommended_instructions"],
        "created_at": _iso(client.created_at),
        "updated_at": _iso(client.updated_at),
        "last_seen_at": _iso(client.last_seen_at),
    }


def _mem_dict(m) -> dict | None:
    if m is None:
        return None
    return {
        "id": str(m.id),
        "namespace": m.namespace,
        "content": m.content,
        "confidence": m.confidence,
        "source": m.source,
        "metadata": m.meta,
    }


def _doc_dict(d) -> dict | None:
    if d is None:
        return None
    return {
        "id": str(d.id),
        "namespace": d.namespace,
        "repo_path": d.repo_path,
        "title": d.title,
        "content": d.raw_content,
    }


def _curated_note_meta(
    metadata: dict | None,
    source_agent_note_ids: list[str] | None,
) -> dict:
    return {
        "metadata": metadata or {},
        "source_agent_note_ids": source_agent_note_ids or [],
    }


def _curated_frontmatter(
    metadata: dict | None,
    source_agent_note_ids: list[str] | None,
) -> dict:
    meta = _curated_note_meta(metadata, source_agent_note_ids)
    return {
        "type": "curated_note",
        "metadata": meta["metadata"] or None,
        "source_agent_note_ids": meta["source_agent_note_ids"] or None,
    }


def _curated_note_dict(d) -> dict | None:
    if d is None:
        return None
    meta = d.meta or {}
    return {
        "id": str(d.id),
        "path": d.repo_path,
        "repo_path": d.repo_path,
        "title": d.title,
        "content": d.raw_content,
        "metadata": meta.get("metadata") or {},
        "source_agent_note_ids": meta.get("source_agent_note_ids") or [],
    }


def _agent_note_dict(note, *, content: str | None = None) -> dict | None:
    if note is None:
        return None
    out = {
        "id": str(note.id),
        "client_slug": note.client_slug,
        "title": note.title,
        "repo_path": note.repo_path,
        "status": note.status,
        "suggested_namespace": note.suggested_namespace,
        "metadata": note.meta or {},
        "outcome": note.outcome or {},
        "error": note.error,
        "created_at": _iso(note.created_at),
        "updated_at": _iso(note.updated_at),
        "claimed_at": _iso(note.claimed_at),
        "completed_at": _iso(note.completed_at),
    }
    if content is not None:
        out["content"] = content
    return out


def _note_link_dict(link) -> dict | None:
    if link is None:
        return None
    return {
        "id": str(link.id),
        "source_document_id": str(link.source_document_id) if link.source_document_id else None,
        "source_path": link.source_path,
        "target": link.target,
        "target_path": link.target_path,
        "alias": link.alias,
        "anchor": link.anchor,
        "raw": link.raw,
        "status": link.status,
        "created_at": _iso(link.created_at),
    }


def _repo_note_path(repo_cache_path: str, repo_path: str, *, allow_agents: bool = False) -> Path:
    if not allow_agents:
        _, note_path = normalize_repo_path(
            repo_cache_path,
            repo_path,
            require_markdown=True,
        )
        return note_path
    repo_root = Path(repo_cache_path).resolve()
    note_path = (repo_root / repo_path).resolve()
    try:
        note_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"agent note path escapes repository: {repo_path}") from exc
    return note_path


def _note_cursor(note) -> str:
    payload = {
        "created_at": note.created_at.isoformat(),
        "id": str(note.id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _parse_note_cursor(cursor: str | None) -> tuple[dt.datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return dt.datetime.fromisoformat(payload["created_at"]), uuid.UUID(payload["id"])
    except (binascii.Error, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def _link_cursor(link) -> str:
    payload = {
        "created_at": link.created_at.isoformat(),
        "id": str(link.id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _parse_link_cursor(cursor: str | None) -> tuple[dt.datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return dt.datetime.fromisoformat(payload["created_at"]), uuid.UUID(payload["id"])
    except (binascii.Error, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def _current_commit_sha(repo_cache_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_cache_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _normalize_repo_prefix(prefix: str | None, *, include_agents: bool) -> str:
    if prefix is None:
        return ""
    raw = str(prefix).replace("\\", "/")
    if raw.startswith("/") or raw.startswith(":"):
        raise ValueError("prefix deve ser relativo")
    if len(raw) >= 3 and raw[1:3] == ":/":
        raise ValueError("prefix deve ser relativo")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("prefix nao pode conter '..'")
        parts.append(part)

    rel = "/".join(parts)
    if not include_agents and (rel == "_agents" or rel.startswith("_agents/")):
        raise ValueError("_agents paths are not available in curated vault tree")
    return rel


async def _get_curated_document_by_id_or_path(session, id_or_path: str):
    try:
        return await repo.get_curated_document(session, id=uuid.UUID(id_or_path))
    except ValueError:
        path = git_writer.validate_curated_note_path(id_or_path)
        return await repo.get_curated_document(session, repo_path=path)


def _candidate_link_paths(target: str) -> list[str]:
    if not target:
        return []
    normalized = target.replace("\\", "/")
    if normalized.endswith(".md"):
        return [normalized]
    return [f"{normalized}.md"]


async def _resolve_link_target_path(session, target: str) -> str | None:
    for candidate in _candidate_link_paths(target):
        try:
            path = git_writer.validate_curated_note_path(candidate)
        except ValueError:
            continue
        if await repo.get_curated_document(session, repo_path=path) is not None:
            return path
    return None


async def _replace_curated_note_links(
    session,
    *,
    document_id,
    repo_path: str,
    content: str,
) -> None:
    links = []
    for link in extract_obsidian_links(content):
        target_path = await _resolve_link_target_path(session, link["target"])
        links.append(
            {
                **link,
                "target_path": target_path,
                "status": "resolved" if target_path else "unresolved",
            }
        )
    await repo.replace_note_links(session, document_id, repo_path, links)


async def _index_curated_note(
    deps: Deps,
    *,
    repo_path: str,
    content: str,
    metadata: dict | None,
    source_agent_note_ids: list[str] | None,
) -> dict:
    meta = _curated_note_meta(metadata, source_agent_note_ids)
    async with deps.session_factory() as s:
        try:
            await pipeline.index_document(
                s,
                deps.embedder,
                deps.llm,
                deps.settings,
                namespace="curated",
                repo_path=repo_path,
                content=content,
                commit_sha=_current_commit_sha(deps.settings.repo_cache_path),
                meta=meta,
                commit=False,
            )
            doc = await repo.get_curated_document(s, repo_path=repo_path)
            if doc is None:
                raise ValueError(f"curated note not found after index: {repo_path}")
            await _replace_curated_note_links(
                s,
                document_id=doc.id,
                repo_path=repo_path,
                content=content,
            )
            out = _curated_note_dict(doc)
            await s.commit()
            return out
        except Exception:
            await s.rollback()
            raise


def _push_curated_note_if_enabled(deps: Deps) -> None:
    if deps.settings.git_push_enabled:
        git_writer.push_repo(deps.settings.repo_cache_path)


def _validate_agent_note_transition(note, *, action: str, allowed_statuses: set[str]) -> None:
    terminal_statuses = {"curated", "rejected", "failed"}
    if note.status in terminal_statuses:
        raise ValueError(f"cannot {action} terminal agent note with status {note.status}")
    if note.status not in allowed_statuses:
        raise ValueError(f"cannot {action} agent note with status {note.status}")


async def _transition_agent_note(
    deps: Deps,
    note_id: str,
    *,
    action: str,
    status: str,
    allowed_statuses: set[str],
    outcome: dict | None = None,
    error: str | None = None,
) -> dict:
    note_uuid = uuid.UUID(note_id)
    async with deps.session_factory() as s:
        note = await repo.update_agent_note_status(
            s,
            note_uuid,
            status,
            outcome=outcome,
            error=error,
            allowed_statuses=allowed_statuses,
        )
        if note is None:
            current = await repo.get_agent_note(s, note_uuid)
            if current is None:
                raise ValueError(f"agent note not found: {note_id}")
            _validate_agent_note_transition(
                current,
                action=action,
                allowed_statuses=allowed_statuses,
            )
            raise ValueError(f"cannot {action} agent note with status {current.status}")
        await s.refresh(note)
        out = _agent_note_dict(note)
        await s.commit()
        return out


# ---------- Memória & recall ----------
async def remember(deps: Deps, namespace: str, messages: list[dict], metadata: dict | None = None) -> dict:
    _require_curator()
    s = deps.settings
    rel = git_writer.write_conversation(
        s.repo_cache_path, s.conversations_dir, namespace, messages,
        timestamp=_now_stamp(), author_name=s.git_author_name,
        author_email=s.git_author_email, push=s.git_push_enabled,
    )
    job_facts = await deps.queue.enqueue(
        JobType.EXTRACT_FACTS.value, {"namespace": namespace, "messages": messages}
    )
    job_index = await deps.queue.enqueue(
        JobType.INDEX_DOCUMENT.value, {"namespace": namespace, "repo_path": rel}
    )
    return {"note_path": rel, "job_ids": [str(job_facts), str(job_index)]}


async def search(
    deps: Deps,
    query: str,
    *legacy_args,
    limit: int | None = 10,
    filters: dict | None = None,
    namespace: str | None = None,
    include_graph: bool = False,
) -> dict:
    _require_client_or_curator()
    if legacy_args:
        if len(legacy_args) > 3:
            raise TypeError("search accepts at most three positional arguments after query")
        legacy_namespace_call = isinstance(legacy_args[0], str) or legacy_args[0] is None
        if legacy_namespace_call:
            namespace = legacy_args[0]
            if len(legacy_args) >= 2:
                limit = legacy_args[1]
            if len(legacy_args) >= 3:
                include_graph = legacy_args[2]
        else:
            limit = legacy_args[0]
            if len(legacy_args) >= 2:
                filters = legacy_args[1]
            if len(legacy_args) >= 3:
                raise TypeError("search positional public form is query, limit, filters")
    resolved_limit = repo.normalize_search_limit(10 if limit is None else limit)

    async with deps.session_factory() as s:
        return await _search(
            s,
            deps.embedder,
            query,
            limit=resolved_limit,
            filters=filters if isinstance(filters, dict) else None,
            namespace=namespace if isinstance(namespace, str) else None,
            include_graph=include_graph,
        )


async def deep_search(
    deps: Deps,
    query: str,
    *,
    limit: int | None = 10,
    depth: int = 1,
    max_entities: int = 3,
    rel_types: list[str] | None = None,
    filters: dict | None = None,
    namespace: str = "curated",
) -> dict:
    _require_deep_search_principal()
    resolved_limit = repo.normalize_search_limit(10 if limit is None else limit)
    resolved_depth = _bounded_int(depth, name="depth", min_value=1, max_value=3)
    resolved_max_entities = _bounded_int(
        max_entities,
        name="max_entities",
        min_value=1,
        max_value=3,
    )
    resolved_rel_types = None if rel_types == [] else rel_types

    async with deps.session_factory() as s:
        return await _deep_search(
            s,
            deps.embedder,
            deps.llm,
            query,
            limit=resolved_limit,
            depth=resolved_depth,
            max_entities=resolved_max_entities,
            rel_types=resolved_rel_types,
            filters=filters if isinstance(filters, dict) else None,
            namespace=namespace if isinstance(namespace, str) else "curated",
        )


# ---------- Notas curadas ----------
async def create_note(
    deps: Deps,
    path: str,
    content: str,
    metadata: dict | None = None,
    source_agent_note_ids: list[str] | None = None,
) -> dict:
    _require_curator()
    repo_path = git_writer.validate_curated_note_path(path)
    note_path = _repo_note_path(deps.settings.repo_cache_path, repo_path)
    if note_path.exists():
        async with deps.session_factory() as s:
            if await repo.get_document(s, repo_path=repo_path) is not None:
                raise ValueError(f"curated note already exists: {repo_path}")
        existing_content = note_path.read_text(encoding="utf-8")
        existing_frontmatter = git_writer.parse_frontmatter(existing_content)
        recovered = await _index_curated_note(
            deps,
            repo_path=repo_path,
            content=existing_content,
            metadata=existing_frontmatter.get("metadata"),
            source_agent_note_ids=existing_frontmatter.get("source_agent_note_ids"),
        )
        _push_curated_note_if_enabled(deps)
        return recovered

    async with deps.session_factory() as s:
        if await repo.get_document(s, repo_path=repo_path) is not None:
            raise ValueError(f"curated note already exists: {repo_path}")

    written_path = git_writer.write_curated_note(
        deps.settings.repo_cache_path,
        repo_path,
        frontmatter=_curated_frontmatter(metadata, source_agent_note_ids),
        content=content,
        author_name=deps.settings.git_author_name,
        author_email=deps.settings.git_author_email,
        push=False,
        expected_exists=False,
    )
    written_content = _repo_note_path(deps.settings.repo_cache_path, written_path).read_text(
        encoding="utf-8"
    )
    out = await _index_curated_note(
        deps,
        repo_path=written_path,
        content=written_content,
        metadata=metadata,
        source_agent_note_ids=source_agent_note_ids,
    )
    _push_curated_note_if_enabled(deps)
    return out


async def update_note(
    deps: Deps,
    id_or_path: str,
    content: str,
    metadata: dict | None = None,
    source_agent_note_ids: list[str] | None = None,
) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        doc = await _get_curated_document_by_id_or_path(s, id_or_path)
        if doc is None:
            raise ValueError(f"curated note not found: {id_or_path}")
        repo_path = doc.repo_path

    written_path = git_writer.write_curated_note(
        deps.settings.repo_cache_path,
        repo_path,
        frontmatter=_curated_frontmatter(metadata, source_agent_note_ids),
        content=content,
        author_name=deps.settings.git_author_name,
        author_email=deps.settings.git_author_email,
        push=False,
        expected_exists=True,
    )
    written_content = _repo_note_path(deps.settings.repo_cache_path, written_path).read_text(
        encoding="utf-8"
    )
    out = await _index_curated_note(
        deps,
        repo_path=written_path,
        content=written_content,
        metadata=metadata,
        source_agent_note_ids=source_agent_note_ids,
    )
    _push_curated_note_if_enabled(deps)
    return out


async def get_note(deps: Deps, id_or_path: str) -> dict | None:
    _require_client_or_curator()
    async with deps.session_factory() as s:
        doc = await _get_curated_document_by_id_or_path(s, id_or_path)
        return _curated_note_dict(doc)


async def list_vault_tree(
    deps: Deps,
    prefix: str | None = None,
    include_agents: bool = False,
    max_depth: int | None = None,
) -> dict:
    _require_curator()
    if max_depth is not None and max_depth < 1:
        raise ValueError("max_depth must be positive")

    repo_root = Path(deps.settings.repo_cache_path).resolve()
    rel_prefix = _normalize_repo_prefix(prefix, include_agents=include_agents)
    base = (repo_root / rel_prefix).resolve()
    try:
        base.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("prefix deve ficar dentro do repositorio") from exc
    if not base.exists():
        return {"items": []}

    def should_skip(rel: str) -> bool:
        return (
            rel == ".git"
            or rel.startswith(".git/")
            or (not include_agents and (rel == "_agents" or rel.startswith("_agents/")))
        )

    candidates = [base] if base.is_file() else list(base.rglob("*"))
    items = []
    for candidate in sorted(candidates, key=lambda p: p.relative_to(repo_root).as_posix()):
        rel = candidate.relative_to(repo_root).as_posix()
        if should_skip(rel):
            continue
        if candidate == base and not rel_prefix:
            continue
        relative_to_base = candidate.relative_to(base).as_posix()
        if relative_to_base == ".":
            depth = 1
        else:
            depth = len(relative_to_base.split("/"))
        if max_depth is not None and depth > max_depth:
            continue
        if candidate.is_dir():
            items.append({"type": "directory", "path": rel})
        elif candidate.suffix == ".md":
            items.append({"type": "note", "path": rel})
    return {"items": items}


async def list_unresolved_links(
    deps: Deps,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    _require_curator()
    bounded_limit = repo.normalize_search_limit(limit)
    after = _parse_link_cursor(cursor)
    async with deps.session_factory() as s:
        links = await repo.list_unresolved_links(s, limit=bounded_limit + 1, after=after)
    page = links[:bounded_limit]
    next_cursor = _link_cursor(page[-1]) if len(links) > bounded_limit and page else None
    return {"items": [_note_link_dict(link) for link in page], "next_cursor": next_cursor}


async def resolve_note_link(deps: Deps, link_id: str, target_path: str) -> dict:
    _require_curator()
    link_uuid = uuid.UUID(link_id)
    async with deps.session_factory() as s:
        target = await _get_curated_document_by_id_or_path(s, target_path)
        if target is None:
            raise ValueError(f"curated note not found: {target_path}")
        link = await repo.resolve_note_link(s, link_uuid, target.repo_path)
        if link is None:
            raise ValueError(f"note link not found: {link_id}")
        out = _note_link_dict(link)
        await s.commit()
        return out


async def submit_agent_note(
    deps: Deps,
    title: str | None = None,
    content: str | None = None,
    messages: list[dict] | None = None,
    suggested_namespace: str | None = None,
    metadata: dict | None = None,
) -> dict:
    principal = _require_client()
    if not content and not messages:
        raise ValueError("content or messages required")

    result: dict
    async with deps.session_factory() as s:
        try:
            client = await repo.get_agent_client(s, slug=principal.slug)
            if client is None:
                raise ValueError(f"active agent client not found: {principal.slug}")
            if client.status != "active":
                raise ValueError(f"agent client disabled: {principal.slug}")
            if "submit_agent_note" not in (client.permissions or []):
                raise PermissionError("submit_agent_note permission required")

            note = await repo.create_agent_note(
                s,
                client_id=client.id,
                client_slug=client.slug,
                title=title,
                repo_path=f"pending/{client.slug}/{uuid.uuid4()}",
                suggested_namespace=suggested_namespace,
                meta=metadata,
                status="pending",
            )
            note_id = str(note.id)
            repo_path = git_writer.write_agent_note(
                deps.settings.repo_cache_path,
                inbox_dir=deps.settings.agent_inbox_dir,
                client_slug=client.slug,
                client_name=client.name,
                note_id=note_id,
                timestamp=_now_stamp(),
                title=title,
                content=content,
                messages=messages,
                suggested_namespace=suggested_namespace,
                metadata=metadata,
                author_name=deps.settings.git_author_name,
                author_email=deps.settings.git_author_email,
                push=False,
            )
            note.repo_path = repo_path
            await s.flush()

            event_payload = {
                "event_type": "agent_note.created",
                "type": "agent_note.created",
                "agent_note": {
                    "id": note_id,
                    "client_slug": client.slug,
                    "repo_path": repo_path,
                },
            }
            event = await repo.create_outbox_event(s, "agent_note.created", event_payload)
            event.payload = {"event_id": str(event.id), **event_payload}
            await s.flush()
            await s.commit()
            result = {
                "note_id": note_id,
                "repo_path": repo_path,
                "status": note.status,
                "event_id": str(event.id),
            }
        except Exception:
            await s.rollback()
            raise

    if deps.settings.git_push_enabled:
        git_writer.push_repo(deps.settings.repo_cache_path)
    return result


async def list_agent_notes(
    deps: Deps,
    status: str | None = None,
    client_slug: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    _require_curator()
    if limit < 1:
        raise ValueError("limit must be positive")
    before = _parse_note_cursor(cursor)
    async with deps.session_factory() as s:
        notes = await repo.list_agent_notes(
            s,
            status=status,
            client_slug=client_slug,
            limit=limit + 1,
            before=before,
        )
    page = notes[:limit]
    next_cursor = _note_cursor(page[-1]) if len(notes) > limit and page else None
    return {"items": [_agent_note_dict(note) for note in page], "next_cursor": next_cursor}


async def get_agent_note(deps: Deps, note_id: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(note_id))
        if note is None:
            return None
        content = _repo_note_path(
            deps.settings.repo_cache_path,
            note.repo_path,
            allow_agents=True,
        ).read_text(encoding="utf-8")
        return _agent_note_dict(note, content=content)


async def claim_agent_note(deps: Deps, note_id: str) -> dict:
    _require_curator()
    return await _transition_agent_note(
        deps,
        note_id,
        action="claim",
        status="in_review",
        allowed_statuses={"pending", "in_review"},
    )


async def complete_agent_note(
    deps: Deps,
    note_id: str,
    outcome: dict | None = None,
) -> dict:
    _require_curator()
    return await _transition_agent_note(
        deps,
        note_id,
        action="complete",
        status="curated",
        allowed_statuses={"pending", "in_review"},
        outcome=outcome,
    )


async def reject_agent_note(
    deps: Deps,
    note_id: str,
    reason: str | None = None,
) -> dict:
    _require_curator()
    outcome = {"reason": reason} if reason is not None else {}
    return await _transition_agent_note(
        deps,
        note_id,
        action="reject",
        status="rejected",
        allowed_statuses={"pending", "in_review"},
        outcome=outcome,
    )


async def fail_agent_note(
    deps: Deps,
    note_id: str,
    error: str | None = None,
) -> dict:
    _require_curator()
    return await _transition_agent_note(
        deps,
        note_id,
        action="fail",
        status="failed",
        allowed_statuses={"pending", "in_review"},
        error=error,
    )


async def get_memory(deps: Deps, id: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        return _mem_dict(await repo.get_memory(s, uuid.UUID(id)))


async def list_memories(deps: Deps, namespace: str | None = None) -> list[dict]:
    _require_curator()
    async with deps.session_factory() as s:
        return [_mem_dict(m) for m in await repo.list_memories(s, namespace)]


async def update_memory(deps: Deps, id: str, content: str | None = None) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        embedding = None
        if content is not None:
            (embedding,) = await deps.embedder.embed([content])
        m = await repo.update_memory(s, uuid.UUID(id), content=content, embedding=embedding)
        await s.commit()
        return _mem_dict(m)


async def move_memory(deps: Deps, id: str, namespace: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        m = await repo.move_memory(s, uuid.UUID(id), namespace)
        await s.commit()
        return _mem_dict(m)


async def delete_memory(deps: Deps, id: str) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        ok = await repo.delete_memory(s, uuid.UUID(id))
        await s.commit()
        return {"deleted": ok}


async def merge_memories(deps: Deps, ids: list[str], into: str | None = None) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        target = await repo.merge_memories(
            s, [uuid.UUID(i) for i in ids], uuid.UUID(into) if into else None
        )
        await s.commit()
        return {"into": str(target)}


# ---------- Documentos ----------
async def get_document(deps: Deps, id_or_path: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        try:
            doc = await repo.get_document(s, id=uuid.UUID(id_or_path))
        except ValueError:
            doc = await repo.get_document(s, repo_path=id_or_path)
        return _doc_dict(doc)


async def list_documents(deps: Deps, namespace: str | None = None) -> list[dict]:
    _require_curator()
    async with deps.session_factory() as s:
        return [_doc_dict(d) for d in await repo.list_documents(s, namespace)]


async def reindex(deps: Deps, repo_path: str, namespace: str) -> dict:
    _require_curator()
    job_id = await deps.queue.enqueue(
        JobType.REINDEX.value, {"namespace": namespace, "repo_path": repo_path}
    )
    return {"job_id": str(job_id)}


# ---------- Agent clients ----------
async def create_agent_client(
    deps: Deps,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    capture_policy: str | None = None,
    recommended_instructions: str | None = None,
    metadata: dict | None = None,
) -> dict:
    _require_curator()
    key = _require_token_encryption_key(deps.settings)
    client_slug = git_writer.slugify(slug or name, fallback="client")
    token = auth.generate_client_token(client_slug)
    token_prefix = _token_prefix(token, client_slug)
    token_hash = auth.hash_token(token)
    permissions = ["search", "get_note", "submit_agent_note"]

    async with deps.session_factory() as s:
        try:
            if await repo.get_agent_client(s, slug=client_slug) is not None:
                raise ValueError(f"agent client already exists: {client_slug}")
            client = await repo.create_agent_client(
                s,
                slug=client_slug,
                name=name,
                description=description,
                token_prefix=token_prefix,
                token_hash=token_hash,
                token_encrypted=auth.encrypt_token(token, key),
                permissions=permissions,
                meta=_stored_client_meta(
                    metadata=metadata,
                    capture_policy=capture_policy,
                    recommended_instructions=recommended_instructions,
                ),
            )
            if client.token_hash != token_hash:
                raise ValueError(f"agent client already exists: {client_slug}")
            profile_path = git_writer.write_agent_client_profile(
                deps.settings.repo_cache_path,
                inbox_dir=deps.settings.agent_inbox_dir,
                client_slug=client.slug,
                client_name=client.name,
                token_prefix=token_prefix,
                token=token,
                description=description,
                capture_policy=capture_policy,
                recommended_instructions=recommended_instructions,
                metadata=metadata,
                author_name=deps.settings.git_author_name,
                author_email=deps.settings.git_author_email,
                push=False,
            )
            out = _agent_client_dict(client)
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    if deps.settings.git_push_enabled:
        git_writer.push_repo(deps.settings.repo_cache_path)
    out.update({"token": token, "profile_path": profile_path})
    return out


async def list_agent_clients(deps: Deps) -> list[dict]:
    _require_curator()
    async with deps.session_factory() as s:
        return [_agent_client_dict(c) for c in await repo.list_agent_clients(s)]


async def get_agent_client(deps: Deps, slug: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        return _agent_client_dict(await repo.get_agent_client(s, slug=slug))


async def reveal_agent_client_token(deps: Deps, slug: str) -> dict:
    _require_curator()
    key = _require_token_encryption_key(deps.settings)
    async with deps.session_factory() as s:
        client = await repo.get_agent_client(s, slug=slug)
        if client is None:
            raise ValueError(f"agent client not found: {slug}")
        return {
            "slug": client.slug,
            "token": auth.decrypt_token(client.token_encrypted, key),
            "token_prefix": client.token_prefix,
        }


async def rotate_agent_client_token(deps: Deps, slug: str) -> dict:
    _require_curator()
    key = _require_token_encryption_key(deps.settings)
    async with deps.session_factory() as s:
        try:
            client = await repo.get_agent_client(s, slug=slug)
            if client is None:
                raise ValueError(f"agent client not found: {slug}")
            token = auth.generate_client_token(client.slug)
            token_prefix = _token_prefix(token, client.slug)
            client = await repo.update_agent_client_token(
                s,
                slug=client.slug,
                token_prefix=token_prefix,
                token_hash=auth.hash_token(token),
                token_encrypted=auth.encrypt_token(token, key),
            )
            await s.refresh(client)
            fields = _profile_fields(client)
            profile_path = git_writer.write_agent_client_profile(
                deps.settings.repo_cache_path,
                inbox_dir=deps.settings.agent_inbox_dir,
                client_slug=client.slug,
                client_name=client.name,
                token_prefix=token_prefix,
                token=token,
                description=client.description,
                capture_policy=fields["capture_policy"],
                recommended_instructions=fields["recommended_instructions"],
                metadata=fields["metadata"],
                author_name=deps.settings.git_author_name,
                author_email=deps.settings.git_author_email,
                push=False,
            )
            out = _agent_client_dict(client)
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    if deps.settings.git_push_enabled:
        git_writer.push_repo(deps.settings.repo_cache_path)
    out.update({"token": token, "profile_path": profile_path})
    return out


async def disable_agent_client(deps: Deps, slug: str) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        client = await repo.disable_agent_client(s, slug)
        if client is None:
            raise ValueError(f"agent client not found: {slug}")
        await s.refresh(client)
        await s.commit()
        out = _agent_client_dict(client)
        out["disabled"] = True
        return out


# ---------- Grafo ----------
async def get_entity(deps: Deps, name: str, namespace: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        return await age.get_entity(s, name, namespace)


async def search_entities(deps: Deps, query: str, namespace: str) -> list[dict]:
    _require_curator()
    async with deps.session_factory() as s:
        return await age.search_entities(s, query, namespace)


async def get_related(deps: Deps, entity: str, namespace: str, depth: int = 1) -> list[dict]:
    _require_curator()
    async with deps.session_factory() as s:
        return await age.get_related(s, entity, namespace, depth)


async def update_entity(deps: Deps, name: str, namespace: str, props: dict) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        await age.update_entity(s, name, namespace, props)
        return {"updated": True}


async def merge_entities(deps: Deps, sources: list[str], into: str, namespace: str) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        await age.merge_entities(s, sources, into, namespace)
        return {"into": into}


async def delete_entity(deps: Deps, name: str, namespace: str) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        await age.delete_entity(s, name, namespace)
        return {"deleted": True}


# ---------- Namespaces ----------
async def create_namespace(deps: Deps, name: str, description: str | None = None) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        ns = await repo.create_namespace(s, name, description)
        await s.commit()
        return {"name": ns.name, "description": ns.description}


async def list_namespaces(deps: Deps) -> list[dict]:
    _require_curator()
    async with deps.session_factory() as s:
        return [{"name": n.name, "description": n.description} for n in await repo.list_namespaces(s)]
