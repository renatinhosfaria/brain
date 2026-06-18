import json
from collections.abc import Iterable
import re

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


async def ensure_graph(session: AsyncSession) -> None:
    await _prepare(session)
    exists = (await session.execute(text("SELECT 1 FROM ag_graph WHERE name='brain'"))).first()
    if not exists:
        await session.execute(text("SELECT create_graph('brain')"))
    await session.commit()


async def upsert_entity(
    session: AsyncSession, name: str, type: str, namespace: str, props: dict | None = None
) -> None:
    await _prepare(session)
    props = props or {}
    source_doc = props.get("source_doc")
    source_memory = props.get("source_memory")
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MERGE (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.type = {_lit(type)}, n.props = {_lit(props)}, "
        f"n.source_doc = {_lit(source_doc)}, "
        f"n.source_memory = {_lit(source_memory)} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def upsert_relation(
    session: AsyncSession, source: str, target: str, rel_type: str, namespace: str
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


async def search_entities(session: AsyncSession, query: str, namespace: str) -> list[dict]:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        f"WHERE toLower(n.name) CONTAINS toLower({_lit(query)}) "
        f"RETURN n.name, n.type $cy$) AS (name agtype, type agtype)"
    )
    rows = (await session.execute(text(q))).all()
    return [{"name": _unwrap(n), "type": _unwrap(t)} for n, t in rows]


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
    seeds: list[str],
    namespace: str,
    depth: int = 1,
    rel_types: list[str] | None = None,
    limit: int = 50,
) -> dict:
    await _prepare(session)
    bounded_depth = max(1, int(depth))
    bounded_limit = max(1, int(limit))
    allowed_types = set(_dedupe_strings(rel_types or []))
    seed_names = sorted(_dedupe_strings(seeds))
    relationships: list[dict] = []
    relationship_keys: set[tuple[str, str, str, str, int]] = set()
    relationship_entities: dict[tuple[str, str, str, str, int], tuple[dict, dict]] = {}

    allowed_types_literal = _lit(sorted(allowed_types))
    should_stop = False

    for seed in seed_names:
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
                f"(s:Entity {{name: {_lit(seed)}, namespace: {_lit(namespace)}}})",
            ]
            for idx, edge in enumerate(edge_vars):
                target = "n" if idx == path_depth - 1 else intermediate_nodes[idx]
                parts.append(f"-[{edge}:REL]-({target}:Entity)")
            pattern = f"{''.join(parts)}"
            rel_type_filters = (
                [f"{edge}.type IN {allowed_types_literal}" for edge in edge_vars]
                if allowed_types
                else []
            )
            where_clause = ""
            if rel_type_filters:
                where_clause = "WHERE " + " AND ".join(rel_type_filters) + " "
            order_by_expr = ", ".join(["n.name"] + [f"{edge}.type" for edge in edge_vars])

            q = (
                f"SELECT * FROM cypher('brain', $cy$ "
                f"MATCH p = {pattern} "
                f"{where_clause}"
                f"WITH p, n "
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
                node_by_id = {node.get("id"): node for node in nodes}
                node_payload_by_id: dict[object, dict] = {}
                node_index_by_id: dict[object, int] = {}
                for idx, node in enumerate(nodes):
                    node_id = node.get("id")
                    if node_id not in node_index_by_id:
                        node_index_by_id[node_id] = idx

                    payload = _entity_payload(node, seed=seed, depth=node_index_by_id.get(node_id, len(nodes)))
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

                    from_node = node_by_id.get(rel.get("start_id")) or node_by_id.get(rel.get("startid"))
                    to_node = node_by_id.get(rel.get("end_id")) or node_by_id.get(rel.get("endid"))
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

                for hop, from_name, to_name, rel_type, rel_start_id, rel_startid, rel_end_id, rel_endid in parsed_rels:
                    payload = {
                        "from": from_name,
                        "to": to_name,
                        "type": rel_type,
                        "seed": seed,
                        "depth": hop,
                    }
                    key = (
                        payload["from"],
                        payload["to"],
                        payload["type"],
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

    relationships.sort(key=lambda r: (r["seed"], r["depth"], r["from"], r["to"], r["type"]))
    limited_relationships = relationships[:bounded_limit]

    entities_by_key: dict[tuple[str, str], dict] = {}
    for rel in limited_relationships:
        rel_key = (
            rel["from"],
            rel["to"],
            rel["type"],
            rel["seed"],
            rel["depth"],
        )
        relation_entities = relationship_entities.get(rel_key)
        if relation_entities is None:
            continue
        for entity in relation_entities:
            existing = entities_by_key.get((entity["name"], namespace))
            if existing is None:
                entities_by_key[(entity["name"], namespace)] = entity
                continue

            if entity["depth"] < existing["depth"]:
                entities_by_key[(entity["name"], namespace)] = entity
            elif entity["depth"] == existing["depth"] and entity["seed"] < existing["seed"]:
                entities_by_key[(entity["name"], namespace)] = entity

    entities = list(entities_by_key.values())
    entities.sort(key=lambda e: (e["seed"], e["depth"], e["name"]))
    return {"entities": entities, "relationships": limited_relationships}


async def update_entity(
    session: AsyncSession, name: str, namespace: str, props: dict
) -> None:
    await _prepare(session)
    source_doc = props.get("source_doc")
    source_memory = props.get("source_memory")
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.props = {_lit(props)}, n.source_doc = {_lit(source_doc)}, "
        f"n.source_memory = {_lit(source_memory)} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
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
    session: AsyncSession, repo_path: str, namespace: str
) -> None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        f"WHERE n.source_doc = {_lit(repo_path)} "
        f"DETACH DELETE n $cy$) AS (v agtype)"
    )
    await session.execute(text(q))
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
