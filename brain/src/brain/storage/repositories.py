import uuid

from sqlalchemy import delete, select

from brain.storage.models import Chunk, Document, Memory, Namespace


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
async def search_chunks(
    session, query_embedding: list[float], namespace: str | None, limit: int
) -> list[dict]:
    dist = Chunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(Chunk.text, dist, Document.repo_path, Document.namespace)
        .join(Document, Chunk.document_id == Document.id)
    )
    if namespace:
        stmt = stmt.where(Document.namespace == namespace)
    stmt = stmt.order_by(dist).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "text": r.text,
            "score": 1.0 - float(r.distance),
            "source": "document",
            "ref": r.repo_path,
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
