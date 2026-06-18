import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain.storage.models import (
    AgentClient,
    AgentNote,
    Chunk,
    Document,
    Memory,
    Namespace,
    NoteLink,
    OutboxEvent,
)


@dataclass(frozen=True)
class OutboxClaimToken:
    attempts: int
    locked_at: dt.datetime | None
    locked_by: str | None


# ---------- Documentos ----------
async def upsert_document(
    session,
    *,
    namespace: str,
    repo_path: str,
    title: str | None,
    raw_content: str,
    content_hash: str,
    commit_sha: str | None,
    meta: dict | None = None,
) -> Document:
    doc = (
        await session.execute(select(Document).where(Document.repo_path == repo_path))
    ).scalar_one_or_none()
    if doc is None:
        doc = Document(repo_path=repo_path)
        session.add(doc)
    doc.namespace = namespace
    doc.title = title
    doc.raw_content = raw_content
    doc.content_hash = content_hash
    doc.commit_sha = commit_sha
    if meta is not None:
        doc.meta = meta
    elif doc.meta is None:
        doc.meta = {}
    await session.flush()
    return doc


async def replace_chunks(
    session, document_id: uuid.UUID, chunks: list[dict], embeddings: list[list[float]]
) -> None:
    await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
    for ch, emb in zip(chunks, embeddings, strict=True):
        session.add(
            Chunk(
                document_id=document_id,
                ordinal=ch["ordinal"],
                text=ch["text"],
                embedding=emb,
                token_count=ch["token_count"],
            )
        )
    await session.flush()


async def get_document(
    session, *, id: uuid.UUID | None = None, repo_path: str | None = None
) -> Document | None:
    stmt = select(Document)
    if id is not None:
        stmt = stmt.where(Document.id == id)
    elif repo_path is not None:
        stmt = stmt.where(Document.repo_path == repo_path)
    else:
        return None
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_curated_document(
    session, *, id: uuid.UUID | None = None, repo_path: str | None = None
) -> Document | None:
    doc = await get_document(session, id=id, repo_path=repo_path)
    if doc is None or doc.namespace != "curated" or doc.repo_path.startswith("_agents/"):
        return None
    return doc


async def list_documents(session, namespace: str | None = None) -> list[Document]:
    stmt = select(Document)
    if namespace:
        stmt = stmt.where(Document.namespace == namespace)
    return list((await session.execute(stmt.order_by(Document.repo_path))).scalars().all())


async def delete_document_by_path(session, repo_path: str) -> bool:
    doc = await get_document(session, repo_path=repo_path)
    if doc is None:
        return False
    await session.delete(doc)  # cascade remove chunks
    await session.flush()
    return True


# ---------- Memórias ----------
async def add_memory(
    session,
    *,
    namespace: str,
    content: str,
    embedding: list[float],
    confidence: float = 1.0,
    source: str = "conversation",
    meta: dict | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> Memory:
    mem = Memory(
        namespace=namespace,
        content=content,
        embedding=embedding,
        confidence=confidence,
        source=source,
        meta=meta or {},
        supersedes_id=supersedes_id,
    )
    session.add(mem)
    await session.flush()
    return mem


async def get_memory(session, id: uuid.UUID) -> Memory | None:
    return (await session.execute(select(Memory).where(Memory.id == id))).scalar_one_or_none()


async def list_memories(session, namespace: str | None = None) -> list[Memory]:
    stmt = select(Memory)
    if namespace:
        stmt = stmt.where(Memory.namespace == namespace)
    return list((await session.execute(stmt.order_by(Memory.created_at.desc()))).scalars().all())


async def update_memory(
    session, id: uuid.UUID, *, content: str | None = None, embedding: list[float] | None = None
) -> Memory | None:
    mem = await get_memory(session, id)
    if mem is None:
        return None
    if content is not None:
        mem.content = content
    if embedding is not None:
        mem.embedding = embedding
    await session.flush()
    return mem


async def move_memory(session, id: uuid.UUID, namespace: str) -> Memory | None:
    mem = await get_memory(session, id)
    if mem is None:
        return None
    mem.namespace = namespace
    await session.flush()
    return mem


async def delete_memory(session, id: uuid.UUID) -> bool:
    mem = await get_memory(session, id)
    if mem is None:
        return False
    await session.delete(mem)
    await session.flush()
    return True


async def merge_memories(
    session, ids: list[uuid.UUID], into: uuid.UUID | None = None
) -> uuid.UUID:
    target = into or ids[0]
    for mid in ids:
        if mid == target:
            continue
        await delete_memory(session, mid)
    return target


# ---------- Busca vetorial ----------
MAX_SEARCH_LIMIT = 50


def normalize_search_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit deve ser um inteiro positivo")
    if limit < 1:
        raise ValueError("limit deve ser positivo")
    return min(limit, MAX_SEARCH_LIMIT)


def _normalize_search_path_prefix(filters: dict | None) -> str | None:
    if not filters or not filters.get("path_prefix"):
        return None

    raw = str(filters["path_prefix"]).replace("\\", "/")
    if raw.startswith("/") or raw.startswith(":"):
        raise ValueError("path_prefix deve ser relativo")
    if len(raw) >= 3 and raw[1:3] == ":/":
        raise ValueError("path_prefix deve ser relativo")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path_prefix nao pode conter '..'")
        parts.append(part)

    rel = "/".join(parts)
    if raw.endswith("/") and rel:
        rel = f"{rel}/"
    blocked = rel.rstrip("/")
    if blocked == "_agents" or blocked.startswith("_agents/"):
        raise ValueError("path_prefix nao pode apontar para _agents/")
    if "%" in rel or "_" in rel:
        raise ValueError("path_prefix nao pode conter '%' ou '_'")
    return rel or None


async def search_chunks(
    session,
    query_embedding: list[float],
    namespace: str | None,
    limit: int,
    filters: dict | None = None,
) -> list[dict]:
    bounded_limit = normalize_search_limit(limit)
    path_prefix = _normalize_search_path_prefix(filters)
    dist = Chunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(Chunk.text, dist, Document.id, Document.repo_path, Document.namespace)
        .join(Document, Chunk.document_id == Document.id)
        .where(and_(Document.repo_path != "_agents", ~Document.repo_path.startswith("_agents/")))
    )
    if namespace:
        stmt = stmt.where(Document.namespace == namespace)
    if path_prefix:
        stmt = stmt.where(Document.repo_path.startswith(path_prefix))
    stmt = stmt.order_by(dist).limit(bounded_limit)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": str(r.id),
            "text": r.text,
            "score": 1.0 - float(r.distance),
            "source": "document",
            "ref": r.repo_path,
            "path": r.repo_path,
            "repo_path": r.repo_path,
            "namespace": r.namespace,
        }
        for r in rows
    ]


async def search_memories(
    session, query_embedding: list[float], namespace: str | None, limit: int
) -> list[dict]:
    dist = Memory.embedding.cosine_distance(query_embedding).label("distance")
    stmt = select(Memory.id, Memory.content, dist, Memory.namespace)
    if namespace:
        stmt = stmt.where(Memory.namespace == namespace)
    stmt = stmt.order_by(dist).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "text": r.content,
            "score": 1.0 - float(r.distance),
            "source": "memory",
            "ref": str(r.id),
            "namespace": r.namespace,
        }
        for r in rows
    ]


# ---------- Namespaces ----------
async def create_namespace(session, name: str, description: str | None = None) -> Namespace:
    ns = (
        await session.execute(select(Namespace).where(Namespace.name == name))
    ).scalar_one_or_none()
    if ns is None:
        ns = Namespace(name=name, description=description)
        session.add(ns)
        await session.flush()
    return ns


async def list_namespaces(session) -> list[Namespace]:
    return list((await session.execute(select(Namespace).order_by(Namespace.name))).scalars().all())


# ---------- Agent clients ----------
async def create_agent_client(
    session,
    *,
    slug: str,
    name: str,
    description: str | None,
    token_prefix: str,
    token_hash: str,
    token_encrypted: str,
    permissions: list[str] | None = None,
    meta: dict | None = None,
) -> AgentClient:
    stmt = (
        pg_insert(AgentClient)
        .values(
            slug=slug,
            name=name,
            description=description,
            token_prefix=token_prefix,
            token_hash=token_hash,
            token_encrypted=token_encrypted,
            permissions=permissions or [],
            meta=meta or {},
        )
        .on_conflict_do_nothing(index_elements=[AgentClient.slug])
        .returning(AgentClient.id)
    )
    await session.execute(stmt)
    client = await get_agent_client(session, slug=slug)
    if client is None:
        raise RuntimeError(f"Agent client insert did not return slug {slug!r}")
    return client


async def get_agent_client(session, *, slug: str) -> AgentClient | None:
    return (
        await session.execute(select(AgentClient).where(AgentClient.slug == slug))
    ).scalar_one_or_none()


async def get_agent_client_by_token_hash(session, token_hash: str) -> AgentClient | None:
    return (
        await session.execute(select(AgentClient).where(AgentClient.token_hash == token_hash))
    ).scalar_one_or_none()


async def list_agent_clients(session) -> list[AgentClient]:
    return list(
        (await session.execute(select(AgentClient).order_by(AgentClient.slug))).scalars().all()
    )


async def update_agent_client_token(
    session,
    *,
    slug: str,
    token_prefix: str,
    token_hash: str,
    token_encrypted: str,
) -> AgentClient | None:
    client = await get_agent_client(session, slug=slug)
    if client is None:
        return None
    client.token_prefix = token_prefix
    client.token_hash = token_hash
    client.token_encrypted = token_encrypted
    await session.flush()
    return client


async def disable_agent_client(session, slug: str) -> AgentClient | None:
    client = await get_agent_client(session, slug=slug)
    if client is None:
        return None
    client.status = "disabled"
    await session.flush()
    return client


async def touch_agent_client_seen(session, slug: str) -> AgentClient | None:
    client = await get_agent_client(session, slug=slug)
    if client is None:
        return None
    client.last_seen_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return client


# ---------- Agent notes ----------
async def create_agent_note(
    session,
    *,
    client_id: uuid.UUID,
    client_slug: str,
    title: str | None,
    repo_path: str,
    suggested_namespace: str | None = None,
    meta: dict | None = None,
    status: str = "pending",
) -> AgentNote:
    note = AgentNote(
        client_id=client_id,
        client_slug=client_slug,
        title=title,
        repo_path=repo_path,
        status=status,
        suggested_namespace=suggested_namespace,
        meta=meta or {},
    )
    session.add(note)
    await session.flush()
    return note


async def get_agent_note(session, id: uuid.UUID) -> AgentNote | None:
    return (
        await session.execute(select(AgentNote).where(AgentNote.id == id))
    ).scalar_one_or_none()


async def list_agent_notes(
    session,
    status: str | None = None,
    client_slug: str | None = None,
    limit: int = 50,
    before: tuple[dt.datetime, uuid.UUID] | None = None,
) -> list[AgentNote]:
    stmt = select(AgentNote)
    if status is not None:
        stmt = stmt.where(AgentNote.status == status)
    if client_slug is not None:
        stmt = stmt.where(AgentNote.client_slug == client_slug)
    if before is not None:
        created_at, note_id = before
        stmt = stmt.where(
            or_(
                AgentNote.created_at < created_at,
                and_(AgentNote.created_at == created_at, AgentNote.id < note_id),
            )
        )
    stmt = stmt.order_by(AgentNote.created_at.desc(), AgentNote.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def update_agent_note_status(
    session,
    id: uuid.UUID,
    status: str,
    outcome: dict | None = None,
    error: str | None = None,
    allowed_statuses: set[str] | None = None,
) -> AgentNote | None:
    stmt = select(AgentNote).where(AgentNote.id == id)
    if allowed_statuses is not None:
        stmt = stmt.where(AgentNote.status.in_(allowed_statuses))
    note = (await session.execute(stmt.with_for_update())).scalar_one_or_none()
    if note is None:
        return None
    note.status = status
    if outcome is not None:
        note.outcome = outcome
    note.error = error
    now = dt.datetime.now(dt.UTC)
    if status == "in_review" and note.claimed_at is None:
        note.claimed_at = now
    if status in {"curated", "rejected", "failed"}:
        note.completed_at = now
    await session.flush()
    return note


# ---------- Outbox ----------
async def create_outbox_event(session, type: str, payload: dict) -> OutboxEvent:
    event = OutboxEvent(type=type, payload=payload)
    session.add(event)
    await session.flush()
    return event


async def claim_next_outbox_event(
    session,
    now: dt.datetime,
    worker_id: str,
    stale_before: dt.datetime | None = None,
) -> OutboxEvent | None:
    pending_or_retrying_due = and_(
        OutboxEvent.status.in_(["pending", "retrying"]),
        or_(OutboxEvent.run_after.is_(None), OutboxEvent.run_after <= now),
    )
    claimable = [pending_or_retrying_due]
    if stale_before is not None:
        claimable.append(
            and_(
                OutboxEvent.status == "running",
                OutboxEvent.locked_at.is_not(None),
                OutboxEvent.locked_at <= stale_before,
            )
        )

    stmt = (
        select(OutboxEvent)
        .where(or_(*claimable))
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    event = (await session.execute(stmt)).scalar_one_or_none()
    if event is None:
        return None
    event.status = "running"
    event.attempts = (event.attempts or 0) + 1
    event.locked_at = now
    event.locked_by = worker_id
    event.run_after = None
    await session.flush()
    return event


def outbox_claim_token(event: OutboxEvent) -> OutboxClaimToken:
    return OutboxClaimToken(
        attempts=event.attempts,
        locked_at=event.locked_at,
        locked_by=event.locked_by,
    )


async def mark_outbox_delivered(
    session,
    id: uuid.UUID,
    *,
    claim: OutboxClaimToken,
) -> OutboxEvent | None:
    event = await _get_outbox_event_for_claim(session, id, claim)
    if event is None:
        return None
    event.status = "delivered"
    event.locked_at = None
    event.locked_by = None
    await session.flush()
    return event


async def mark_outbox_retrying(
    session,
    id: uuid.UUID,
    error: str,
    run_after: dt.datetime,
    *,
    claim: OutboxClaimToken,
) -> OutboxEvent | None:
    event = await _get_outbox_event_for_claim(session, id, claim)
    if event is None:
        return None
    event.status = "retrying"
    event.last_error = error
    event.run_after = run_after
    event.locked_at = None
    event.locked_by = None
    await session.flush()
    return event


async def mark_outbox_failed(
    session,
    id: uuid.UUID,
    error: str,
    *,
    claim: OutboxClaimToken,
) -> OutboxEvent | None:
    event = await _get_outbox_event_for_claim(session, id, claim)
    if event is None:
        return None
    event.status = "failed"
    event.last_error = error
    event.locked_at = None
    event.locked_by = None
    await session.flush()
    return event


async def _get_outbox_event(session, id: uuid.UUID) -> OutboxEvent | None:
    return (
        await session.execute(select(OutboxEvent).where(OutboxEvent.id == id))
    ).scalar_one_or_none()


async def _get_outbox_event_for_claim(
    session,
    id: uuid.UUID,
    claim: OutboxClaimToken,
) -> OutboxEvent | None:
    return (
        await session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == id,
                OutboxEvent.status == "running",
                OutboxEvent.attempts == claim.attempts,
                OutboxEvent.locked_at == claim.locked_at,
                OutboxEvent.locked_by == claim.locked_by,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


# ---------- Note links ----------
async def replace_note_links(
    session,
    source_document_id: uuid.UUID | None,
    source_path: str,
    links: list[dict],
) -> list[NoteLink]:
    filters = [NoteLink.source_path == source_path]
    if source_document_id is not None:
        filters.append(NoteLink.source_document_id == source_document_id)
    await session.execute(delete(NoteLink).where(or_(*filters)))

    created = []
    for link in links:
        target_path = link.get("target_path")
        note_link = NoteLink(
            source_document_id=source_document_id,
            source_path=source_path,
            target=link["target"],
            target_path=target_path,
            alias=link.get("alias"),
            anchor=link.get("anchor"),
            raw=link.get("raw") or f"[[{link['target']}]]",
            status=link.get("status") or ("resolved" if target_path else "unresolved"),
        )
        session.add(note_link)
        created.append(note_link)
    await session.flush()
    return created


async def list_unresolved_links(session, limit: int = 50) -> list[NoteLink]:
    stmt = (
        select(NoteLink)
        .where(NoteLink.status == "unresolved")
        .order_by(NoteLink.created_at, NoteLink.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def resolve_note_link(
    session,
    link_id: uuid.UUID,
    target_path: str,
) -> NoteLink | None:
    link = (
        await session.execute(select(NoteLink).where(NoteLink.id == link_id))
    ).scalar_one_or_none()
    if link is None:
        return None
    link.target_path = target_path
    link.status = "resolved"
    await session.flush()
    return link
