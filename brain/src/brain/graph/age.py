import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GRAPH = "brain"


async def _prepare(session: AsyncSession) -> None:
    await session.execute(text("LOAD 'age'"))
    await session.execute(text('SET search_path = ag_catalog, "$user", public'))


def _lit(value: object) -> str:
    """Literal seguro para Cypher via JSON (escapa aspas/barras)."""
    return json.dumps(value, ensure_ascii=False)


def _unwrap(agtype_value: object):
    s = str(agtype_value)
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


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
    props_json = json.dumps(props or {}, ensure_ascii=False)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MERGE (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.type = {_lit(type)}, n.props = {_lit(props_json)} "
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


async def update_entity(
    session: AsyncSession, name: str, namespace: str, props: dict
) -> None:
    await _prepare(session)
    props_json = json.dumps(props, ensure_ascii=False)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.props = {_lit(props_json)} RETURN n $cy$) AS (n agtype)"
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
