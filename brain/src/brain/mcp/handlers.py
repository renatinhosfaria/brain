import datetime as dt
import uuid
from dataclasses import dataclass
from pathlib import Path

from brain import auth
from brain.graph import age
from brain.ingestion import git_writer
from brain.queue.base import JobType
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


def _repo_note_path(repo_cache_path: str, repo_path: str) -> Path:
    repo_root = Path(repo_cache_path).resolve()
    note_path = (repo_root / repo_path).resolve()
    try:
        note_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"agent note path escapes repository: {repo_path}") from exc
    return note_path


def _parse_note_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor)
    except ValueError as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if offset < 0:
        raise ValueError("cursor must be a non-negative integer")
    return offset


def _validate_agent_note_transition(note, *, action: str, allowed_statuses: set[str]) -> None:
    terminal_statuses = {"curated", "rejected", "failed"}
    if note.status in terminal_statuses:
        raise ValueError(f"cannot {action} terminal agent note with status {note.status}")
    if note.status not in allowed_statuses:
        raise ValueError(f"cannot {action} agent note with status {note.status}")


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


async def search(deps: Deps, query: str, namespace: str | None = None,
                 limit: int = 10, include_graph: bool = False) -> dict:
    _require_client_or_curator()
    async with deps.session_factory() as s:
        return await _search(s, deps.embedder, query, namespace=namespace,
                             limit=limit, include_graph=include_graph)


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
    offset = _parse_note_cursor(cursor)
    async with deps.session_factory() as s:
        notes = await repo.list_agent_notes(
            s,
            status=status,
            client_slug=client_slug,
            limit=offset + limit + 1,
        )
    page = notes[offset : offset + limit]
    next_cursor = str(offset + limit) if len(notes) > offset + limit else None
    return {"items": [_agent_note_dict(note) for note in page], "next_cursor": next_cursor}


async def get_agent_note(deps: Deps, note_id: str) -> dict | None:
    _require_curator()
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(note_id))
        if note is None:
            return None
        content = _repo_note_path(deps.settings.repo_cache_path, note.repo_path).read_text(
            encoding="utf-8"
        )
        return _agent_note_dict(note, content=content)


async def claim_agent_note(deps: Deps, note_id: str) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(note_id))
        if note is None:
            raise ValueError(f"agent note not found: {note_id}")
        _validate_agent_note_transition(
            note,
            action="claim",
            allowed_statuses={"pending", "in_review"},
        )
        note = await repo.update_agent_note_status(s, note.id, "in_review")
        await s.refresh(note)
        out = _agent_note_dict(note)
        await s.commit()
        return out


async def complete_agent_note(
    deps: Deps,
    note_id: str,
    outcome: dict | None = None,
) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(note_id))
        if note is None:
            raise ValueError(f"agent note not found: {note_id}")
        _validate_agent_note_transition(
            note,
            action="complete",
            allowed_statuses={"pending", "in_review"},
        )
        note = await repo.update_agent_note_status(s, note.id, "curated", outcome=outcome)
        await s.refresh(note)
        out = _agent_note_dict(note)
        await s.commit()
        return out


async def reject_agent_note(
    deps: Deps,
    note_id: str,
    reason: str | None = None,
) -> dict:
    _require_curator()
    outcome = {"reason": reason} if reason is not None else {}
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(note_id))
        if note is None:
            raise ValueError(f"agent note not found: {note_id}")
        _validate_agent_note_transition(
            note,
            action="reject",
            allowed_statuses={"pending", "in_review"},
        )
        note = await repo.update_agent_note_status(s, note.id, "rejected", outcome=outcome)
        await s.refresh(note)
        out = _agent_note_dict(note)
        await s.commit()
        return out


async def fail_agent_note(
    deps: Deps,
    note_id: str,
    error: str | None = None,
) -> dict:
    _require_curator()
    async with deps.session_factory() as s:
        note = await repo.get_agent_note(s, uuid.UUID(note_id))
        if note is None:
            raise ValueError(f"agent note not found: {note_id}")
        _validate_agent_note_transition(
            note,
            action="fail",
            allowed_statuses={"pending", "in_review"},
        )
        note = await repo.update_agent_note_status(s, note.id, "failed", error=error)
        await s.refresh(note)
        out = _agent_note_dict(note)
        await s.commit()
        return out


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
        await s.commit()
        out = _agent_client_dict(client)

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
        push=deps.settings.git_push_enabled,
    )
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
        await s.commit()
        out = _agent_client_dict(client)
        description = client.description
        client_name = client.name
        client_slug = client.slug

    profile_path = git_writer.write_agent_client_profile(
        deps.settings.repo_cache_path,
        inbox_dir=deps.settings.agent_inbox_dir,
        client_slug=client_slug,
        client_name=client_name,
        token_prefix=token_prefix,
        token=token,
        description=description,
        capture_policy=fields["capture_policy"],
        recommended_instructions=fields["recommended_instructions"],
        metadata=fields["metadata"],
        author_name=deps.settings.git_author_name,
        author_email=deps.settings.git_author_email,
        push=deps.settings.git_push_enabled,
    )
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
