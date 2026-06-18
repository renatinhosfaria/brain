from brain.graph import age
from brain.storage import repositories as repo


async def search(
    session,
    embedder,
    query: str,
    *,
    limit: int = 10,
    filters: dict | None = None,
    namespace: str | None = None,
    include_graph: bool = False,
) -> dict:
    limit = repo.normalize_search_limit(limit)
    source_filter = (filters or {}).get("source")
    if source_filter not in (None, "document", "curated", "note"):
        return {"results": [], "graph": []}
    (qvec,) = await embedder.embed([query])
    chunk_hits = await repo.search_chunks(session, qvec, "curated", limit, filters=filters)
    results = sorted(chunk_hits, key=lambda r: r["score"], reverse=True)[:limit]

    graph: list[dict] = []
    if include_graph and namespace:
        for ent in (await age.search_entities(session, query, namespace))[:3]:
            graph.extend(await age.get_related(session, ent["name"], namespace))

    return {"results": results, "graph": graph}
