from brain.graph import age
from brain.storage import repositories as repo


async def search(
    session,
    embedder,
    query: str,
    *,
    namespace: str | None = None,
    limit: int = 10,
    include_graph: bool = False,
) -> dict:
    (qvec,) = await embedder.embed([query])
    chunk_hits = await repo.search_chunks(session, qvec, namespace, limit)
    mem_hits = await repo.search_memories(session, qvec, namespace, limit)
    results = sorted(chunk_hits + mem_hits, key=lambda r: r["score"], reverse=True)[:limit]

    graph: list[dict] = []
    if include_graph and namespace:
        for ent in (await age.search_entities(session, query, namespace))[:3]:
            graph.extend(await age.get_related(session, ent["name"], namespace))

    return {"results": results, "graph": graph}
