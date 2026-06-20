import json
from collections.abc import Iterable
import re
import unicodedata

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GRAPH = "brain"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _prepare(session: AsyncSession) -> None:
    await session.execute(text("LOAD 'age'"))
    await session.execute(text('SET search_path = ag_catalog, "$user", public'))


def _key_lit(key: object) -> str:
    text_key = str(key)
    if _IDENT.match(text_key):
        return text_key
    return "`" + text_key.replace("`", "``") + "`"


def _lit(value: object) -> str:
    """Literal seguro para Cypher via JSON (escapa aspas/barras)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_key_lit(k)}: {_lit(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_lit(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _unwrap(agtype_value: object):
    s = str(agtype_value)
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


def _split_agtype_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw or raw == "[]":
        return []
    if raw[0] == "[" and raw[-1] == "]":
        raw = raw[1:-1]

    items: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escape = False
    for idx, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            item = raw[start:idx].strip()
            if item:
                items.append(item)
            start = idx + 1
    tail = raw[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parse_agtype_entity(value: object) -> dict:
    raw = str(value).strip()
    raw = re.sub(r"::(vertex|edge)$", "", raw)
    return json.loads(raw)


def _parse_agtype_entity_list(value: object) -> list[dict]:
    return [_parse_agtype_entity(item) for item in _split_agtype_list(str(value))]


def _props(entity: dict) -> dict:
    props = entity.get("properties")
    return props if isinstance(props, dict) else {}


def _entity_payload(node: dict, *, seed: str, depth: int) -> dict:
    props = _props(node)
    return {
        "name": props.get("name"),
        "type": props.get("type"),
        "namespace": props.get("namespace"),
        "seed": seed,
        "depth": depth,
    }


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _normalize_match_text(value: object) -> str:
    if value is None:
        return ""
    text_value = str(value).casefold()
    text_value = "".join(
        " " if unicodedata.category(ch) == "Pd" else ch for ch in text_value
    )
    decomposed = unicodedata.normalize("NFKD", text_value)
    without_accents = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    cleaned = "".join(
        ch if ch.isalnum() or ch.isspace() or ch == "." else " "
        for ch in without_accents
    )
    return " ".join(cleaned.split())


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(
        value, (bytes, bytearray, dict)
    ):
        values = value
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _props_with_search_text(name: str, props: dict | None) -> dict:
    enriched = dict(props or {})
    name_normalized = _normalize_match_text(name)
    enriched["name_normalized"] = name_normalized

    alias_values = _as_string_list(enriched.get("aliases")) + _as_string_list(
        enriched.get("aliases_normalized")
    )
    aliases_normalized = _dedupe_strings(
        normalized
        for value in alias_values
        if (normalized := _normalize_match_text(value))
    )
    if aliases_normalized:
        enriched["aliases_normalized"] = aliases_normalized
    enriched["aliases_search_text_normalized"] = " ".join(aliases_normalized)

    tag_values = _as_string_list(enriched.get("tags")) + _as_string_list(
        enriched.get("tags_normalized")
    )
    tags_normalized = _dedupe_strings(
        normalized
        for value in tag_values
        if (normalized := _normalize_match_text(value))
    )
    if tags_normalized:
        enriched["tags_normalized"] = tags_normalized
    enriched["tags_search_text_normalized"] = " ".join(tags_normalized)

    path_values = []
    for key in (
        "source_doc",
        "source_doc_normalized",
        "repo_path",
        "repo_path_normalized",
        "path",
        "path_normalized",
    ):
        path_values.extend(_as_string_list(enriched.get(key)))
    path_values_normalized = _dedupe_strings(
        normalized
        for value in path_values
        if (normalized := _normalize_match_text(value))
    )
    if enriched.get("source_doc") is not None:
        enriched["source_doc_normalized"] = _normalize_match_text(
            enriched.get("source_doc")
        )
    if enriched.get("repo_path") is not None:
        enriched["repo_path_normalized"] = _normalize_match_text(
            enriched.get("repo_path")
        )
    if enriched.get("path") is not None:
        enriched["path_normalized"] = _normalize_match_text(enriched.get("path"))
    enriched["path_search_text_normalized"] = " ".join(path_values_normalized)

    values: list[str] = [
        name_normalized,
        *aliases_normalized,
        *tags_normalized,
        *path_values_normalized,
    ]
    for key in (
        "search_text_normalized",
    ):
        values.extend(_as_string_list(enriched.get(key)))

    normalized_values = [
        normalized
        for value in values
        if (normalized := _normalize_match_text(value))
    ]
    enriched["search_text_normalized"] = " ".join(_dedupe_strings(normalized_values))
    return enriched


def _normalize_seed_entries(
    seeds: list[str] | list[dict],
    namespace: str | None,
) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for seed in seeds:
        if isinstance(seed, dict):
            name = str(seed.get("name") or "").strip()
            seed_namespace = seed.get("namespace")
            seed_namespace = (
                str(seed_namespace).strip() if seed_namespace is not None else ""
            )
        else:
            name = str(seed or "").strip()
            seed_namespace = namespace or ""

        if not name or not seed_namespace:
            continue
        if namespace is not None and seed_namespace != namespace:
            continue

        key = (name, seed_namespace)
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "namespace": seed_namespace})
    result.sort(key=lambda item: (item["namespace"], item["name"]))
    return result


async def ensure_graph(session: AsyncSession, *, commit: bool = True) -> None:
    await _prepare(session)
    exists = (await session.execute(text("SELECT 1 FROM ag_graph WHERE name='brain'"))).first()
    if not exists:
        await session.execute(text("SELECT create_graph('brain')"))
    if commit:
        await session.commit()


async def upsert_entity(
    session: AsyncSession,
    name: str,
    type: str,
    namespace: str,
    props: dict | None = None,
    *,
    commit: bool = True,
) -> None:
    await _prepare(session)
    props = _props_with_search_text(name, props)
    source_doc = props.get("source_doc")
    source_memory = props.get("source_memory")
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MERGE (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.type = {_lit(type)}, n.props = {_lit(props)}, "
        f"n.source_doc = {_lit(source_doc)}, "
        f"n.source_memory = {_lit(source_memory)}, "
        f"n.name_normalized = {_lit(props.get('name_normalized'))}, "
        f"n.search_text_normalized = {_lit(props.get('search_text_normalized'))}, "
        f"n.aliases_search_text_normalized = {_lit(props.get('aliases_search_text_normalized'))}, "
        f"n.tags_search_text_normalized = {_lit(props.get('tags_search_text_normalized'))}, "
        f"n.path_search_text_normalized = {_lit(props.get('path_search_text_normalized'))}, "
        f"n.source_doc_normalized = {_lit(props.get('source_doc_normalized'))}, "
        f"n.repo_path_normalized = {_lit(props.get('repo_path_normalized'))} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    if commit:
        await session.commit()


async def upsert_relation(
    session: AsyncSession,
    source: str,
    target: str,
    rel_type: str,
    namespace: str,
    *,
    commit: bool = True,
) -> None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (a:Entity {{name: {_lit(source)}, namespace: {_lit(namespace)}}}), "
        f"(b:Entity {{name: {_lit(target)}, namespace: {_lit(namespace)}}}) "
        f"MERGE (a)-[r:REL {{type: {_lit(rel_type)}}}]->(b) "
        f"RETURN r $cy$) AS (r agtype)"
    )
    await session.execute(text(q))
    if commit:
        await session.commit()


async def get_entity(session: AsyncSession, name: str, namespace: str) -> dict | None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"RETURN n.name, n.type, n.props $cy$) AS (name agtype, type agtype, props agtype)"
    )
    row = (await session.execute(text(q))).first()
    if row is None:
        return None
    return {"name": _unwrap(row[0]), "type": _unwrap(row[1]), "props": _unwrap(row[2])}


async def find_entity_by_source_doc(
    session: AsyncSession,
    namespace: str,
    repo_path: str | None = None,
    *,
    source_doc: str | None = None,
    document_id: str | None = None,
) -> dict | None:
    await _prepare(session)
    source_path = repo_path or source_doc
    source_filters = []
    if source_path is not None:
        source_filters.extend(
            [
                f"n.source_doc = {_lit(source_path)}",
                f"n.props.source_doc = {_lit(source_path)}",
                f"n.props.repo_path = {_lit(source_path)}",
            ]
        )
    if document_id is not None:
        source_filters.append(f"n.props.document_id = {_lit(document_id)}")
    if not source_filters:
        return None
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        f"WHERE {' OR '.join(source_filters)} "
        f"RETURN n.name, n.type, n.namespace, n.props "
        f"ORDER BY n.name "
        f"LIMIT 1 $cy$) AS (name agtype, type agtype, namespace agtype, props agtype)"
    )
    row = (await session.execute(text(q))).first()
    if row is None:
        return None
    props = _unwrap(row[3])
    return {
        "name": _unwrap(row[0]),
        "type": _unwrap(row[1]),
        "namespace": _unwrap(row[2]),
        "props": props if isinstance(props, dict) else {},
    }


async def search_entities(
    session: AsyncSession,
    query: str,
    namespace: str | None,
    limit: int | None = None,
) -> list[dict]:
    await _prepare(session)
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit deve ser um inteiro positivo")
    query_normalized = _normalize_match_text(query)
    candidate_limit = max(limit * 20, 100) if limit is not None else 500
    namespace_match = (
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        if namespace is not None
        else "MATCH (n:Entity) "
    )

    async def fetch_candidates(where_clause: str = "") -> list[tuple]:
        q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"{namespace_match}"
            f"{where_clause}"
            f"RETURN n.name, n.type, n.namespace, n.props, n.source_doc "
            f"ORDER BY n.namespace, n.name, n.type "
            f"LIMIT {candidate_limit} "
            f"$cy$) AS (name agtype, type agtype, namespace agtype, "
            f"props agtype, source_doc agtype)"
        )
        return (await session.execute(text(q))).all()

    if query_normalized:
        query_lit = _lit(query)
        normalized_lit = _lit(query_normalized)
        where_clauses = [
            (
                f"WHERE toLower(n.name) = toLower({query_lit}) "
                f"OR n.name_normalized = {normalized_lit} "
                f"OR n.props.name_normalized = {normalized_lit} "
            ),
            (
                f"WHERE toLower(n.name) STARTS WITH toLower({query_lit}) "
                f"OR n.name_normalized STARTS WITH {normalized_lit} "
                f"OR n.props.name_normalized STARTS WITH {normalized_lit} "
            ),
            (
                f"WHERE toLower(n.name) CONTAINS toLower({query_lit}) "
                f"OR n.name_normalized CONTAINS {normalized_lit} "
                f"OR n.props.name_normalized CONTAINS {normalized_lit} "
            ),
            (
                f"WHERE n.aliases_search_text_normalized CONTAINS {normalized_lit} "
                f"OR n.props.aliases_search_text_normalized CONTAINS {normalized_lit} "
            ),
            (
                f"WHERE n.tags_search_text_normalized CONTAINS {normalized_lit} "
                f"OR n.props.tags_search_text_normalized CONTAINS {normalized_lit} "
            ),
            (
                f"WHERE n.search_text_normalized CONTAINS {normalized_lit} "
                f"OR n.props.search_text_normalized CONTAINS {normalized_lit} "
            ),
            (
                f"WHERE toLower(n.source_doc) CONTAINS toLower({query_lit}) "
                f"OR n.source_doc_normalized CONTAINS {normalized_lit} "
                f"OR n.repo_path_normalized CONTAINS {normalized_lit} "
                f"OR n.path_search_text_normalized CONTAINS {normalized_lit} "
                f"OR n.props.source_doc_normalized CONTAINS {normalized_lit} "
                f"OR n.props.repo_path_normalized CONTAINS {normalized_lit} "
                f"OR n.props.path_normalized CONTAINS {normalized_lit} "
                f"OR n.props.path_search_text_normalized CONTAINS {normalized_lit} "
            ),
        ]
        rows = []
        seen_candidates: set[tuple[object, object]] = set()
        for where_clause in where_clauses:
            for row in await fetch_candidates(where_clause):
                key = (_unwrap(row[0]), _unwrap(row[2]))
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                rows.append(row)
    else:
        rows = await fetch_candidates()

    ranked: list[tuple[int, str, str, str, str, dict]] = []
    for raw_name, raw_type, raw_namespace, raw_props, raw_source_doc in rows:
        name = _unwrap(raw_name)
        type_ = _unwrap(raw_type)
        namespace_ = _unwrap(raw_namespace)
        props = _unwrap(raw_props)
        if not isinstance(props, dict):
            props = {}

        name_normalized = _normalize_match_text(name)
        alias_values = [
            _normalize_match_text(alias)
            for alias in (
                _as_string_list(props.get("aliases"))
                + _as_string_list(props.get("aliases_normalized"))
            )
        ]
        tag_values = [
            _normalize_match_text(tag)
            for tag in (
                _as_string_list(props.get("tags"))
                + _as_string_list(props.get("tags_normalized"))
            )
        ]
        path_values = [
            _normalize_match_text(path)
            for path in _as_string_list(
                [
                    props.get("source_doc"),
                    props.get("source_doc_normalized"),
                    props.get("repo_path"),
                    props.get("repo_path_normalized"),
                    props.get("path"),
                    _unwrap(raw_source_doc),
                ]
            )
        ]
        search_values = [
            _normalize_match_text(value)
            for value in _as_string_list(props.get("search_text_normalized"))
        ]

        rank: int | None
        if not query_normalized:
            rank = 6
        elif name_normalized == query_normalized:
            rank = 1
        elif query_normalized in alias_values:
            rank = 2
        elif query_normalized in tag_values:
            rank = 3
        elif name_normalized.startswith(query_normalized) or any(
            alias.startswith(query_normalized) for alias in alias_values
        ):
            rank = 4
        elif query_normalized in name_normalized or any(
            query_normalized in alias for alias in alias_values
        ):
            rank = 5
        elif any(
            query_normalized in value
            for value in tag_values + path_values + search_values
        ):
            rank = 6
        else:
            continue

        payload = {"name": name, "type": type_, "namespace": namespace_}
        ranked.append(
            (
                rank,
                name_normalized,
                str(namespace_),
                str(name),
                str(type_),
                payload,
            )
        )

    ranked.sort(key=lambda item: item[:5])
    results = [payload for *_, payload in ranked]
    return results[:limit] if limit is not None else results


async def get_related(
    session: AsyncSession, name: str, namespace: str, depth: int = 1
) -> list[dict]:
    await _prepare(session)
    depth = max(1, int(depth))
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (a:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}})"
        f"-[*1..{depth}]-(b:Entity) "
        f"RETURN DISTINCT b.name, b.type $cy$) AS (name agtype, type agtype)"
    )
    rows = (await session.execute(text(q))).all()
    return [{"name": _unwrap(n), "type": _unwrap(t)} for n, t in rows]


async def get_relationship_paths(
    session: AsyncSession,
    seeds: list[str] | list[dict],
    namespace: str | None = None,
    depth: int = 1,
    rel_types: list[str] | None = None,
    limit: int = 50,
) -> dict:
    await _prepare(session)
    bounded_depth = max(1, int(depth))
    bounded_limit = max(1, int(limit))
    allowed_types = set(_dedupe_strings(rel_types or []))
    seed_entries = _normalize_seed_entries(seeds, namespace)
    relationships: list[dict] = []
    relationship_keys: set[tuple[str, str, str, str, str, int]] = set()
    relationship_entities: dict[
        tuple[str, str, str, str, str, int], tuple[dict, dict]
    ] = {}

    allowed_types_literal = _lit(sorted(allowed_types))
    should_stop = False

    for seed_entry in seed_entries:
        seed = seed_entry["name"]
        seed_namespace = seed_entry["namespace"]
        if should_stop:
            break
        for path_depth in range(1, bounded_depth + 1):
            remaining = bounded_limit - len(relationships)
            if remaining <= 0:
                should_stop = True
                break

            edge_vars = [f"r{idx}" for idx in range(path_depth)]
            intermediate_nodes = [f"v{idx}" for idx in range(path_depth - 1)]
            parts = [
                f"(s:Entity {{name: {_lit(seed)}, namespace: {_lit(seed_namespace)}}})",
            ]
            for idx, edge in enumerate(edge_vars):
                target = "n" if idx == path_depth - 1 else intermediate_nodes[idx]
                parts.append(
                    f"-[{edge}:REL]-({target}:Entity {{namespace: {_lit(seed_namespace)}}})"
                )
            pattern = f"{''.join(parts)}"
            rel_type_filters = (
                [f"{edge}.type IN {allowed_types_literal}" for edge in edge_vars]
                if allowed_types
                else []
            )
            where_clause = ""
            if rel_type_filters:
                where_clause = "WHERE " + " AND ".join(rel_type_filters) + " "
            # Deterministic pagination before Python slicing: names (including intermediaries)
            # and edge types are used to break ties when multiple paths share n.name.
            node_names_expr = [f"{node}.name" for node in intermediate_nodes]
            rel_type_expr = [f"{edge}.type" for edge in edge_vars]
            order_by_expr = ", ".join(["n.name"] + node_names_expr + rel_type_expr)
            with_expr = ", ".join(["p", "n"] + edge_vars + intermediate_nodes)

            q = (
                f"SELECT * FROM cypher('brain', $cy$ "
                f"MATCH p = {pattern} "
                f"{where_clause}"
                f"WITH {with_expr} "
                f"ORDER BY {order_by_expr} "
                f"LIMIT {remaining} "
                f"RETURN nodes(p), relationships(p) $cy$) AS (nodes agtype, rels agtype)"
            )
            rows = (await session.execute(text(q))).all()
            for nodes_value, rels_value in rows:
                if should_stop:
                    break
                nodes = _parse_agtype_entity_list(nodes_value)
                rels = _parse_agtype_entity_list(rels_value)
                if any(_props(node).get("namespace") != seed_namespace for node in nodes):
                    continue
                node_by_id = {node.get("id"): node for node in nodes}
                node_payload_by_id: dict[object, dict] = {}
                node_index_by_id: dict[object, int] = {}
                for idx, node in enumerate(nodes):
                    node_id = node.get("id")
                    if node_id not in node_index_by_id:
                        node_index_by_id[node_id] = idx

                    payload = _entity_payload(
                        node,
                        seed=seed,
                        depth=node_index_by_id.get(node_id, len(nodes)),
                    )
                    name = payload["name"]
                    if name:
                        node_payload_by_id[node_id] = payload

                parsed_rels: list[
                    tuple[
                        int, str, str, str, object | None, object | None, object | None, object | None
                    ]
                ] = []
                path_ok = True
                for hop, rel in enumerate(rels, start=1):
                    rel_props = _props(rel)
                    rel_type = rel_props.get("type") or rel.get("label")
                    # Defensivo: o filtro também é revalidado em Python para não depender
                    # de um predicado Cypher por completo.
                    if allowed_types and rel_type not in allowed_types:
                        path_ok = False
                        break

                    from_node = node_by_id.get(rel.get("start_id")) or node_by_id.get(
                        rel.get("startid")
                    )
                    to_node = node_by_id.get(rel.get("end_id")) or node_by_id.get(
                        rel.get("endid")
                    )
                    if from_node is None or to_node is None:
                        path_ok = False
                        break

                    from_name = _props(from_node).get("name")
                    to_name = _props(to_node).get("name")
                    if not from_name or not to_name or not rel_type:
                        path_ok = False
                        break

                    parsed_rels.append(
                        (
                            hop,
                            str(from_name),
                            str(to_name),
                            str(rel_type),
                            rel.get("start_id"),
                            rel.get("startid"),
                            rel.get("end_id"),
                            rel.get("endid"),
                        )
                    )

                if not path_ok:
                    continue

                for (
                    hop,
                    from_name,
                    to_name,
                    rel_type,
                    rel_start_id,
                    rel_startid,
                    rel_end_id,
                    rel_endid,
                ) in parsed_rels:
                    payload = {
                        "from": from_name,
                        "to": to_name,
                        "type": rel_type,
                        "namespace": seed_namespace,
                        "seed": seed,
                        "depth": hop,
                    }
                    key = (
                        payload["from"],
                        payload["to"],
                        payload["type"],
                        payload["namespace"],
                        payload["seed"],
                        payload["depth"],
                    )
                    if key in relationship_keys:
                        continue
                    relationship_keys.add(key)
                    relationships.append(payload)
                    from_node_id = rel_start_id if rel_start_id is not None else rel_startid
                    to_node_id = rel_end_id if rel_end_id is not None else rel_endid
                    from_payload = node_payload_by_id.get(from_node_id)
                    to_payload = node_payload_by_id.get(to_node_id)
                    if from_payload is None or to_payload is None:
                        from_node = node_by_id.get(from_node_id)
                        to_node = node_by_id.get(to_node_id)
                        if from_node is not None:
                            from_payload = _entity_payload(
                                from_node,
                                seed=seed,
                                depth=node_index_by_id.get(from_node.get("id"), len(nodes)),
                            )
                        if to_node is not None:
                            to_payload = _entity_payload(
                                to_node,
                                seed=seed,
                                depth=node_index_by_id.get(to_node.get("id"), len(nodes)),
                            )
                    if from_payload is None or to_payload is None:
                        continue
                    relationship_entities[key] = (from_payload, to_payload)
                    if len(relationships) >= bounded_limit:
                        should_stop = True
                        break
                if should_stop:
                    break
            if should_stop:
                break

    relationships.sort(
        key=lambda r: (
            r["namespace"],
            r["seed"],
            r["depth"],
            r["from"],
            r["to"],
            r["type"],
        )
    )
    limited_relationships = relationships[:bounded_limit]

    entities_by_key: dict[tuple[str, str], dict] = {}
    for rel in limited_relationships:
        rel_key = (
            rel["from"],
            rel["to"],
            rel["type"],
            rel["namespace"],
            rel["seed"],
            rel["depth"],
        )
        relation_entities = relationship_entities.get(rel_key)
        if relation_entities is None:
            continue
        for entity in relation_entities:
            entity_namespace = entity.get("namespace")
            existing = entities_by_key.get((entity["name"], entity_namespace))
            if existing is None:
                entities_by_key[(entity["name"], entity_namespace)] = entity
                continue

            if entity["depth"] < existing["depth"]:
                entities_by_key[(entity["name"], entity_namespace)] = entity
            elif entity["depth"] == existing["depth"] and entity["seed"] < existing["seed"]:
                entities_by_key[(entity["name"], entity_namespace)] = entity

    entities = list(entities_by_key.values())
    entities.sort(key=lambda e: (e["namespace"], e["seed"], e["depth"], e["name"]))
    return {"entities": entities, "relationships": limited_relationships}


async def update_entity(
    session: AsyncSession,
    name: str,
    namespace: str,
    props: dict,
    *,
    commit: bool = True,
) -> None:
    await _prepare(session)
    props = _props_with_search_text(name, props)
    source_doc = props.get("source_doc")
    source_memory = props.get("source_memory")
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.props = {_lit(props)}, n.source_doc = {_lit(source_doc)}, "
        f"n.source_memory = {_lit(source_memory)}, "
        f"n.name_normalized = {_lit(props.get('name_normalized'))}, "
        f"n.search_text_normalized = {_lit(props.get('search_text_normalized'))}, "
        f"n.aliases_search_text_normalized = {_lit(props.get('aliases_search_text_normalized'))}, "
        f"n.tags_search_text_normalized = {_lit(props.get('tags_search_text_normalized'))}, "
        f"n.path_search_text_normalized = {_lit(props.get('path_search_text_normalized'))}, "
        f"n.source_doc_normalized = {_lit(props.get('source_doc_normalized'))}, "
        f"n.repo_path_normalized = {_lit(props.get('repo_path_normalized'))} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    if commit:
        await session.commit()


async def update_entity_identity(
    session: AsyncSession,
    current_name: str | None = None,
    namespace: str | None = None,
    *,
    entity: dict | None = None,
    name: str,
    type: str,
    props: dict,
    commit: bool = True,
) -> None:
    await _prepare(session)
    if current_name is None and entity is not None:
        entity_name = entity.get("name")
        current_name = str(entity_name) if entity_name is not None else None
    if namespace is None and entity is not None:
        entity_namespace = entity.get("namespace")
        namespace = str(entity_namespace) if entity_namespace is not None else None
    if not current_name:
        raise ValueError("current_name ou entity['name'] deve ser informado")
    if not namespace:
        raise ValueError("namespace deve ser informado")

    props = _props_with_search_text(name, props)
    source_doc = props.get("source_doc")
    source_memory = props.get("source_memory")
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(current_name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.name = {_lit(name)}, n.type = {_lit(type)}, "
        f"n.props = {_lit(props)}, n.source_doc = {_lit(source_doc)}, "
        f"n.source_memory = {_lit(source_memory)}, "
        f"n.name_normalized = {_lit(props.get('name_normalized'))}, "
        f"n.search_text_normalized = {_lit(props.get('search_text_normalized'))}, "
        f"n.aliases_search_text_normalized = {_lit(props.get('aliases_search_text_normalized'))}, "
        f"n.tags_search_text_normalized = {_lit(props.get('tags_search_text_normalized'))}, "
        f"n.path_search_text_normalized = {_lit(props.get('path_search_text_normalized'))}, "
        f"n.source_doc_normalized = {_lit(props.get('source_doc_normalized'))}, "
        f"n.repo_path_normalized = {_lit(props.get('repo_path_normalized'))} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    if commit:
        await session.commit()


async def delete_entity(session: AsyncSession, name: str, namespace: str) -> None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"DETACH DELETE n $cy$) AS (v agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def delete_entities_by_source_doc(
    session: AsyncSession,
    repo_path: str,
    namespace: str,
    *,
    commit: bool = True,
) -> None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        f"WHERE n.source_doc = {_lit(repo_path)} "
        f"DETACH DELETE n $cy$) AS (v agtype)"
    )
    await session.execute(text(q))
    if commit:
        await session.commit()


async def delete_entities_by_source_memory(
    session: AsyncSession, memory_id: str, namespace: str | None = None
) -> None:
    await _prepare(session)
    namespace_filter = (
        f"n.namespace = {_lit(namespace)} AND "
        if namespace is not None
        else ""
    )
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity) "
        f"WHERE {namespace_filter}"
        f"(n.source_memory = {_lit(memory_id)} OR n.props.source_memory = {_lit(memory_id)}) "
        f"DETACH DELETE n $cy$) AS (v agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def merge_entities(
    session: AsyncSession, sources: list[str], into: str, namespace: str
) -> None:
    """Move relações dos `sources` para `into` e remove os `sources`."""
    await _prepare(session)
    for src in sources:
        if src == into:
            continue
        # relações de saída. A aresta do MERGE recebe uma variável (nr) de
        # propósito: `[:REL ...]` (dois-pontos após `[`) faria o text() do
        # SQLAlchemy tratar `:REL` como bind parameter. Com `nr:REL` o `:` vem
        # após letra e é ignorado pelo parser.
        out_q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH (s:Entity {{name: {_lit(src)}, namespace: {_lit(namespace)}}})-[r:REL]->(o), "
            f"(t:Entity {{name: {_lit(into)}, namespace: {_lit(namespace)}}}) "
            f"MERGE (t)-[nr:REL {{type: r.type}}]->(o) $cy$) AS (v agtype)"
        )
        # relações de entrada
        in_q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH (o)-[r:REL]->(s:Entity {{name: {_lit(src)}, namespace: {_lit(namespace)}}}), "
            f"(t:Entity {{name: {_lit(into)}, namespace: {_lit(namespace)}}}) "
            f"MERGE (o)-[nr:REL {{type: r.type}}]->(t) $cy$) AS (v agtype)"
        )
        del_q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH (s:Entity {{name: {_lit(src)}, namespace: {_lit(namespace)}}}) "
            f"DETACH DELETE s $cy$) AS (v agtype)"
        )
        await session.execute(text(out_q))
        await session.execute(text(in_q))
        await session.execute(text(del_q))
    await session.commit()
