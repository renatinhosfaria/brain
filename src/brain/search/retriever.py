from brain.extraction.query_entities import extract_query_entities
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


def _dedupe_entities(entities: list[dict], max_entities: int) -> list[dict]:
    if isinstance(max_entities, bool) or not isinstance(max_entities, int) or max_entities < 1:
        return []
    seen: set[str] = set()
    result: list[dict] = []
    for entity in entities:
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "type": entity.get("type")})
        if len(result) >= max_entities:
            break
    return result


async def _resolve_seed_entities(
    session,
    query: str,
    namespace: str,
    max_entities: int,
) -> tuple[list[dict], str]:
    direct = _dedupe_entities(await age.search_entities(session, query, namespace), max_entities)
    if direct:
        return direct, "substring"
    return [], "none"


async def _resolve_llm_entities(
    session,
    llm,
    query: str,
    namespace: str,
    max_entities: int,
) -> tuple[list[dict], list[str]]:
    try:
        candidates = await extract_query_entities(llm, query, max_entities)
    except Exception as exc:  # noqa: BLE001
        return [], [f"query entity fallback failed: {exc}"]

    resolved: list[dict] = []
    for candidate in candidates:
        resolved.extend(await age.search_entities(session, candidate, namespace))
        deduped = _dedupe_entities(resolved, max_entities)
        if len(deduped) >= max_entities:
            return deduped, []
    return _dedupe_entities(resolved, max_entities), []


async def deep_search(
    session,
    embedder,
    llm,
    query: str,
    *,
    limit: int = 10,
    depth: int = 1,
    max_entities: int = 3,
    rel_types: list[str] | None = None,
    filters: dict | None = None,
    namespace: str = "curated",
) -> dict:
    limit = repo.normalize_search_limit(limit)
    source_filter = (filters or {}).get("source")
    if source_filter not in (None, "document", "curated", "note"):
        return {
            "query": query,
            "results": [],
            "graph": {"entities": [], "relationships": []},
            "meta": {
                "depth": depth,
                "max_entities": max_entities,
                "seed_strategy": "none",
                "rel_types": rel_types,
                "warnings": [],
            },
        }

    (qvec,) = await embedder.embed([query])
    chunk_hits = await repo.search_chunks(session, qvec, "curated", limit, filters=filters)
    results = sorted(chunk_hits, key=lambda r: r["score"], reverse=True)[:limit]

    warnings: list[str] = []
    seeds, seed_strategy = await _resolve_seed_entities(session, query, namespace, max_entities)
    if not seeds:
        llm_seeds, llm_warnings = await _resolve_llm_entities(
            session,
            llm,
            query,
            namespace,
            max_entities,
        )
        warnings.extend(llm_warnings)
        if llm_seeds:
            seeds = llm_seeds
            seed_strategy = "llm"

    graph = {"entities": [], "relationships": []}
    if seeds:
        graph = await age.get_relationship_paths(
            session,
            [seed["name"] for seed in seeds],
            namespace,
            depth=depth,
            rel_types=rel_types,
            limit=50,
        )
        seed_names = {seed["name"] for seed in seeds}
        for entity in graph["entities"]:
            if entity["depth"] == 0 and entity["name"] in seed_names:
                entity["matched_by"] = seed_strategy
            else:
                entity["matched_by"] = "relationship"

    return {
        "query": query,
        "results": results,
        "graph": graph,
        "meta": {
            "depth": depth,
            "max_entities": max_entities,
            "seed_strategy": seed_strategy,
            "rel_types": rel_types,
            "warnings": warnings,
        },
    }
