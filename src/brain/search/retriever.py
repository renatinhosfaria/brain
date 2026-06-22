from brain.extraction.query_entities import extract_query_entities
from brain.graph import age
from brain.search.rerank import rerank as _rerank
from brain.storage import repositories as repo


async def _ranked_chunks(
    session,
    query: str,
    qvec,
    limit: int,
    filters: dict | None,
    *,
    llm=None,
    rerank_enabled: bool = False,
    rerank_candidates: int = 20,
) -> list[dict]:
    """Busca chunks curados e, quando habilitado, reordena via LLM (top-k → top_n)."""
    do_rerank = rerank_enabled and llm is not None
    pool = repo.normalize_search_limit(max(limit, rerank_candidates)) if do_rerank else limit
    chunk_hits = await repo.search_chunks(session, qvec, "curated", pool, filters=filters)
    ranked = sorted(chunk_hits, key=lambda r: r["score"], reverse=True)
    if do_rerank:
        return await _rerank(llm, query, ranked, top_n=limit)
    return ranked[:limit]


async def search(
    session,
    embedder,
    query: str,
    *,
    limit: int = 10,
    filters: dict | None = None,
    namespace: str | None = None,
    include_graph: bool = False,
    llm=None,
    rerank_enabled: bool = False,
    rerank_candidates: int = 20,
) -> dict:
    limit = repo.normalize_search_limit(limit)
    source_filter = (filters or {}).get("source")
    if source_filter not in (None, "document", "curated", "note"):
        return {"results": [], "graph": []}
    (qvec,) = await embedder.embed([query])
    results = await _ranked_chunks(
        session,
        query,
        qvec,
        limit,
        filters,
        llm=llm,
        rerank_enabled=rerank_enabled,
        rerank_candidates=rerank_candidates,
    )

    graph: list[dict] = []
    if include_graph and namespace:
        for ent in (await age.search_entities(session, query, namespace))[:3]:
            graph.extend(await age.get_related(session, ent["name"], namespace))

    return {"results": results, "graph": graph}


def _dedupe_entities(entities: list[dict], max_entities: int) -> list[dict]:
    max_entities = _valid_max_entities(max_entities)
    if max_entities == 0:
        return []
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for entity in entities:
        name = str(entity.get("name") or "").strip()
        namespace = str(entity.get("namespace") or "").strip()
        if not name or not namespace:
            continue
        key = (name.casefold(), namespace)
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "type": entity.get("type"), "namespace": namespace})
        if len(result) >= max_entities:
            break
    return result


def _valid_max_entities(value) -> int:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 0
    return value


def _graph_namespaces(graph: dict) -> list[str]:
    namespaces = {
        str(item.get("namespace"))
        for collection in (graph.get("entities", []), graph.get("relationships", []))
        for item in collection
        if item.get("namespace")
    }
    return sorted(namespaces)


async def _resolve_seed_entities(
    session,
    query: str,
    namespace: str | None,
    max_entities: int,
) -> tuple[list[dict], str]:
    direct = _dedupe_entities(
        await age.search_entities(session, query, namespace, limit=max_entities),
        max_entities,
    )
    if direct:
        return direct, "substring"
    return [], "none"


async def _resolve_llm_entities(
    session,
    llm,
    query: str,
    namespace: str | None,
    max_entities: int,
) -> tuple[list[dict], list[str]]:
    try:
        candidates = await extract_query_entities(llm, query, max_entities)
    except Exception as exc:  # noqa: BLE001
        return [], [f"query entity fallback failed: {exc}"]

    resolved: list[dict] = []
    for candidate in candidates:
        remaining = max_entities - len(_dedupe_entities(resolved, max_entities))
        if remaining <= 0:
            break
        resolved.extend(await age.search_entities(session, candidate, namespace, limit=remaining))
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
    namespace: str | None = None,
    rerank_enabled: bool = False,
    rerank_candidates: int = 20,
    as_of: str | None = None,
) -> dict:
    limit = repo.normalize_search_limit(limit)
    resolved_max_entities = _valid_max_entities(max_entities)
    namespace_strategy = "all" if namespace is None else "single"
    source_filter = (filters or {}).get("source")
    if source_filter not in (None, "document", "curated", "note"):
        return {
            "query": query,
            "results": [],
            "graph": {"entities": [], "relationships": []},
            "meta": {
                "depth": depth,
                "max_entities": resolved_max_entities,
                "seed_strategy": "none",
                "namespace_strategy": namespace_strategy,
                "namespaces": [],
                "rel_types": rel_types,
                "warnings": [],
            },
        }

    (qvec,) = await embedder.embed([query])
    results = await _ranked_chunks(
        session,
        query,
        qvec,
        limit,
        filters,
        llm=llm,
        rerank_enabled=rerank_enabled,
        rerank_candidates=rerank_candidates,
    )

    if resolved_max_entities == 0:
        return {
            "query": query,
            "results": results,
            "graph": {"entities": [], "relationships": []},
            "meta": {
                "depth": depth,
                "max_entities": resolved_max_entities,
                "seed_strategy": "none",
                "namespace_strategy": namespace_strategy,
                "namespaces": [],
                "rel_types": rel_types,
                "warnings": [],
            },
        }

    warnings: list[str] = []
    seeds, seed_strategy = await _resolve_seed_entities(
        session,
        query,
        namespace,
        resolved_max_entities,
    )
    if not seeds:
        llm_seeds, llm_warnings = await _resolve_llm_entities(
            session,
            llm,
            query,
            namespace,
            resolved_max_entities,
        )
        warnings.extend(llm_warnings)
        if llm_seeds:
            seeds = llm_seeds
            seed_strategy = "llm"

    graph: dict[str, list] = {"entities": [], "relationships": []}
    namespaces: list = []
    if seeds:
        graph = await age.get_relationship_paths(
            session,
            seeds,
            namespace,
            depth=depth,
            rel_types=rel_types,
            limit=50,
            as_of=as_of,
        )
        seed_keys = {(seed["name"], seed["namespace"]) for seed in seeds}
        for entity in graph["entities"]:
            if entity["depth"] == 0 and (entity["name"], entity.get("namespace")) in seed_keys:
                entity["matched_by"] = seed_strategy
            else:
                entity["matched_by"] = "relationship"
        namespaces = _graph_namespaces(graph)

    return {
        "query": query,
        "results": results,
        "graph": graph,
        "meta": {
            "depth": depth,
            "max_entities": resolved_max_entities,
            "seed_strategy": seed_strategy,
            "namespace_strategy": namespace_strategy,
            "namespaces": namespaces,
            "rel_types": rel_types,
            "warnings": warnings,
        },
    }
