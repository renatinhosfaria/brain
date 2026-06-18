from brain.extraction.entities import extract_entities
from brain.extraction.facts import extract_facts
from brain.graph import age
from brain.indexing.chunker import chunk_markdown
from brain.ingestion.git_sync import content_hash
from brain.storage import repositories as repo


def _title(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return None


async def index_document(
    session, embedder, llm, settings, *, namespace, repo_path, content, commit_sha, meta=None
) -> bool:
    """Indexa um documento. Retorna False se foi no-op (conteúdo inalterado)."""
    h = content_hash(content)
    existing = await repo.get_document(session, repo_path=repo_path)
    if existing and existing.content_hash == h:
        return False

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
    chunks = chunk_markdown(content, settings.chunk_max_tokens, settings.chunk_overlap_tokens)
    embeddings = await embedder.embed([c["text"] for c in chunks]) if chunks else []
    await repo.replace_chunks(session, doc.id, chunks, embeddings)

    if llm is not None:
        ents = await extract_entities(llm, content)
        await age.ensure_graph(session)
        for e in ents["entities"]:
            await age.upsert_entity(session, e["name"], e["type"], namespace, {"source_doc": repo_path})
        for r in ents["relations"]:
            await age.upsert_relation(session, r["source"], r["target"], r["type"], namespace)
    await session.commit()
    return True


async def extract_and_store_facts(session, embedder, llm, *, namespace, messages) -> list[dict]:
    facts = await extract_facts(llm, messages)
    for f in facts:
        (emb,) = await embedder.embed([f["content"]])
        await repo.add_memory(
            session,
            namespace=namespace,
            content=f["content"],
            embedding=emb,
            confidence=f["confidence"],
        )
    await session.commit()
    return facts
