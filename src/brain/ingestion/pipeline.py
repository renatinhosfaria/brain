from brain.extraction.entities import extract_entities
from brain.extraction.facts import extract_facts
from brain.graph import age
from brain.indexing.chunker import chunk_markdown
from brain.ingestion.git_sync import content_hash
from brain.ingestion.semantic_entities import upsert_entity_from_curated_document
from brain.storage import repositories as repo


def _title(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return None


async def _sync_curated_semantic_entity(session, doc, *, content: str) -> None:
    metadata = (doc.meta or {}).get("metadata") if isinstance(doc.meta, dict) else None
    await age.ensure_graph(session, commit=False)
    await upsert_entity_from_curated_document(
        session,
        namespace=doc.namespace,
        repo_path=doc.repo_path,
        title=doc.title,
        content=content,
        metadata=metadata if isinstance(metadata, dict) else {},
        document_id=str(doc.id),
    )


async def index_document(
    session,
    embedder,
    llm,
    settings,
    *,
    namespace,
    repo_path,
    content,
    commit_sha,
    meta=None,
    commit: bool = True,
) -> bool:
    """Indexa um documento. Retorna False se foi no-op (conteúdo inalterado)."""
    h = content_hash(content)
    existing = await repo.get_document(session, repo_path=repo_path)
    if existing and existing.content_hash == h:
        next_meta = existing.meta if meta is None else meta
        title = _title(content)
        if (
            existing.namespace != namespace
            or existing.title != title
            or existing.raw_content != content
            or existing.commit_sha != commit_sha
            or existing.meta != next_meta
        ):
            existing = await repo.upsert_document(
                session,
                namespace=namespace,
                repo_path=repo_path,
                title=title,
                raw_content=content,
                content_hash=h,
                commit_sha=commit_sha,
                meta=meta,
            )
        await _sync_curated_semantic_entity(session, existing, content=content)
        if commit:
            await session.commit()
        return False

    await age.ensure_graph(session, commit=False)
    if existing:
        await age.delete_entities_by_source_doc(
            session,
            repo_path,
            existing.namespace,
            commit=False,
        )

    doc = await repo.upsert_document(
        session,
        namespace=namespace,
        repo_path=repo_path,
        title=_title(content),
        raw_content=content,
        content_hash=h,
        commit_sha=commit_sha,
        meta=meta,
    )
    await _sync_curated_semantic_entity(session, doc, content=content)
    chunks = chunk_markdown(content, settings.chunk_max_tokens, settings.chunk_overlap_tokens)
    embeddings = await embedder.embed([c["text"] for c in chunks]) if chunks else []
    await repo.replace_chunks(session, doc.id, chunks, embeddings)

    if llm is not None:
        ents = await extract_entities(llm, content)
        await age.ensure_graph(session, commit=False)
        for e in ents["entities"]:
            await age.upsert_entity(
                session,
                e["name"],
                e["type"],
                namespace,
                {"source_doc": repo_path},
                commit=False,
            )
        for r in ents["relations"]:
            await age.upsert_relation(
                session,
                r["source"],
                r["target"],
                r["type"],
                namespace,
                commit=False,
            )
    if commit:
        await session.commit()
    return True


async def extract_and_store_facts(
    session, embedder, llm, *, namespace, messages, metadata: dict | None = None
) -> list[dict]:
    facts = await extract_facts(llm, messages)
    stored = []
    for f in facts:
        if await repo.get_memory_by_content(session, namespace, f["content"]):
            continue
        (emb,) = await embedder.embed([f["content"]])
        mem = await repo.add_memory(
            session,
            namespace=namespace,
            content=f["content"],
            embedding=emb,
            confidence=f["confidence"],
            meta=metadata or {},
        )
        stored.append(mem)

    if stored:
        await age.ensure_graph(session)
    for mem in stored:
        ents = await extract_entities(llm, mem.content)
        for e in ents["entities"]:
            await age.upsert_entity(
                session,
                e["name"],
                e["type"],
                namespace,
                {"source": "memory", "source_memory": str(mem.id)},
            )
        for r in ents["relations"]:
            await age.upsert_relation(session, r["source"], r["target"], r["type"], namespace)

    await session.commit()
    return facts
