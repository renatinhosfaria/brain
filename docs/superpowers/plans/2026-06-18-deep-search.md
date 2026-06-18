# Deep Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a separate MCP `deep_search` tool that returns curated semantic chunks plus structured Apache AGE relationship context.

**Architecture:** Keep the existing `search` fast path unchanged. Add graph path retrieval in `brain.graph.age`, query-entity fallback in `brain.extraction.query_entities`, orchestration in `brain.search.retriever.deep_search`, and MCP exposure/guardrails in `brain.mcp.handlers` and `brain.mcp.server`.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, SQLAlchemy async, pgvector, Apache AGE Cypher, FastMCP, OpenAI-compatible `LLMClient.complete_json`.

---

## File Structure

- Create: `src/brain/extraction/query_entities.py`  
  Strict LLM fallback for extracting entity names from a user query.

- Modify: `src/brain/graph/age.py`  
  Add agtype entity-list parsing helpers and `get_relationship_paths`.

- Modify: `src/brain/search/retriever.py`  
  Add `deep_search` orchestration while leaving `search` unchanged.

- Modify: `src/brain/mcp/handlers.py`  
  Import the new retriever function, validate public parameters, expose `deep_search`.

- Modify: `src/brain/mcp/server.py`  
  Register the MCP tool with the public schema.

- Modify: `tests/integration/test_graph.py`  
  Integration tests for path entities, relationships, direction, depth, dedupe, filters, and limit.

- Create: `tests/test_query_entities.py`  
  Unit tests for query entity fallback parsing and limiting.

- Modify: `tests/integration/test_retriever.py`  
  Integration tests for vector + graph orchestration and fallback behavior.

- Modify: `tests/integration/test_mcp_handlers.py`  
  Handler and MCP schema tests for `deep_search` without changing `search`.

## Task 1: Graph Relationship Paths

**Files:**
- Modify: `src/brain/graph/age.py`
- Test: `tests/integration/test_graph.py`

- [ ] **Step 1: Add failing graph tests**

Append these tests to `tests/integration/test_graph.py`:

```python
async def test_get_relationship_paths_retorna_entidades_relacoes_direcao_e_depth(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_entity(session, "Vault", "conceito", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await age.upsert_relation(session, "brain", "Vault", "stores", "curated")
    await session.commit()

    out = await age.get_relationship_paths(session, ["brain"], "curated", depth=2)

    assert {"name": "brain", "type": "projeto", "seed": "brain", "depth": 0} in out["entities"]
    assert {"name": "Hermes", "type": "agente", "seed": "brain", "depth": 1} in out["entities"]
    assert {"name": "Vault", "type": "conceito", "seed": "brain", "depth": 1} in out["entities"]
    assert {
        "from": "Hermes",
        "to": "brain",
        "type": "curates",
        "seed": "brain",
        "depth": 1,
    } in out["relationships"]
    assert {
        "from": "brain",
        "to": "Vault",
        "type": "stores",
        "seed": "brain",
        "depth": 1,
    } in out["relationships"]


async def test_get_relationship_paths_filtra_rel_types(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_entity(session, "Vault", "conceito", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await age.upsert_relation(session, "brain", "Vault", "stores", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["brain"],
        "curated",
        depth=2,
        rel_types=["stores"],
    )

    assert {rel["type"] for rel in out["relationships"]} == {"stores"}
    assert {entity["name"] for entity in out["entities"]} == {"brain", "Vault"}


async def test_get_relationship_paths_deduplica_e_respeita_limit(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    for idx in range(3):
        name = f"Entidade {idx}"
        await age.upsert_entity(session, name, "conceito", "curated")
        await age.upsert_relation(session, "brain", name, "relates_to", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["brain", "brain"],
        "curated",
        depth=1,
        limit=2,
    )

    assert len(out["relationships"]) == 2
    assert len({(r["from"], r["to"], r["type"], r["seed"], r["depth"]) for r in out["relationships"]}) == 2
    assert len({(e["name"], e["seed"], e["depth"]) for e in out["entities"]}) == len(out["entities"])
```

- [ ] **Step 2: Run graph tests and confirm failure**

Run:

```bash
uv run pytest tests/integration/test_graph.py -q
```

Expected: FAIL with `AttributeError: module 'brain.graph.age' has no attribute 'get_relationship_paths'`.

- [ ] **Step 3: Implement graph path parsing and traversal**

In `src/brain/graph/age.py`, add these imports near the top:

```python
from collections.abc import Iterable
```

Then add these helpers after `_unwrap`:

```python
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
```

Add `get_relationship_paths` after `get_related`:

```python
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
    seed_names = _dedupe_strings(seeds)

    entity_by_key: dict[tuple[str, str], dict] = {}
    relationships: list[dict] = []
    relationship_keys: set[tuple[str, str, str, str, int]] = set()

    for seed in seed_names:
        if len(relationships) >= bounded_limit:
            break
        q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH p = (s:Entity {{name: {_lit(seed)}, namespace: {_lit(namespace)}}})"
            f"-[*1..{bounded_depth}]-(n:Entity) "
            f"RETURN nodes(p), relationships(p) $cy$) AS (nodes agtype, rels agtype)"
        )
        rows = (await session.execute(text(q))).all()
        for nodes_value, rels_value in rows:
            nodes = _parse_agtype_entity_list(nodes_value)
            rels = _parse_agtype_entity_list(rels_value)
            node_by_id = {node.get("id"): node for node in nodes}

            for hop, node in enumerate(nodes):
                payload = _entity_payload(node, seed=seed, depth=hop)
                name = payload["name"]
                if not name:
                    continue
                key = (str(name), seed)
                existing = entity_by_key.get(key)
                if existing is None or payload["depth"] < existing["depth"]:
                    entity_by_key[key] = payload

            for hop, rel in enumerate(rels, start=1):
                rel_props = _props(rel)
                rel_type = rel_props.get("type") or rel.get("label")
                if allowed_types and rel_type not in allowed_types:
                    continue
                from_node = node_by_id.get(rel.get("start_id")) or node_by_id.get(rel.get("startid"))
                to_node = node_by_id.get(rel.get("end_id")) or node_by_id.get(rel.get("endid"))
                if from_node is None or to_node is None:
                    continue
                from_name = _props(from_node).get("name")
                to_name = _props(to_node).get("name")
                if not from_name or not to_name or not rel_type:
                    continue
                payload = {
                    "from": from_name,
                    "to": to_name,
                    "type": rel_type,
                    "seed": seed,
                    "depth": hop,
                }
                key = (
                    str(payload["from"]),
                    str(payload["to"]),
                    str(payload["type"]),
                    str(payload["seed"]),
                    int(payload["depth"]),
                )
                if key in relationship_keys:
                    continue
                relationship_keys.add(key)
                relationships.append(payload)

                for endpoint, endpoint_depth in ((from_node, max(0, hop - 1)), (to_node, hop)):
                    entity_payload = _entity_payload(endpoint, seed=seed, depth=endpoint_depth)
                    entity_name = entity_payload["name"]
                    if entity_name:
                        entity_key = (str(entity_name), seed)
                        existing = entity_by_key.get(entity_key)
                        if existing is None or entity_payload["depth"] < existing["depth"]:
                            entity_by_key[entity_key] = entity_payload

                if len(relationships) >= bounded_limit:
                    break
            if len(relationships) >= bounded_limit:
                break

    filtered_entity_names = {rel["from"] for rel in relationships} | {rel["to"] for rel in relationships}
    entities = [
        entity
        for entity in entity_by_key.values()
        if entity["depth"] == 0 or entity["name"] in filtered_entity_names
    ]
    entities.sort(key=lambda e: (e["seed"], e["depth"], e["name"]))
    relationships.sort(key=lambda r: (r["seed"], r["depth"], r["from"], r["to"], r["type"]))
    return {"entities": entities, "relationships": relationships}
```

- [ ] **Step 4: Run graph tests**

Run:

```bash
uv run pytest tests/integration/test_graph.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit graph layer**

```bash
git add src/brain/graph/age.py tests/integration/test_graph.py
git commit -m "feat(brain): adiciona caminhos estruturados no grafo"
```

## Task 2: Query Entity Fallback

**Files:**
- Create: `src/brain/extraction/query_entities.py`
- Test: `tests/test_query_entities.py`

- [ ] **Step 1: Add failing unit tests**

Create `tests/test_query_entities.py`:

```python
import pytest

from brain.extraction.query_entities import extract_query_entities


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete_json(self, system, user):
        self.calls.append({"system": system, "user": user})
        return self.payload


@pytest.mark.asyncio
async def test_extract_query_entities_limita_deduplica_e_remove_vazios():
    llm = FakeLLM(
        {
            "entities": [
                "brain",
                {"name": "Hermes"},
                {"name": "brain"},
                {"name": "  "},
                {"name": "Vault"},
            ]
        }
    )

    out = await extract_query_entities(llm, "Como Hermes se relaciona com brain?", 2)

    assert out == ["brain", "Hermes"]
    assert "no maximo 2" in llm.calls[0]["system"]


@pytest.mark.asyncio
async def test_extract_query_entities_sem_llm_retorna_lista_vazia():
    assert await extract_query_entities(None, "brain", 3) == []
```

- [ ] **Step 2: Run query entity tests and confirm failure**

Run:

```bash
uv run pytest tests/test_query_entities.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'brain.extraction.query_entities'`.

- [ ] **Step 3: Implement query entity extractor**

Create `src/brain/extraction/query_entities.py`:

```python
def _system_prompt(max_entities: int) -> str:
    return (
        "Extraia no maximo "
        f"{max_entities} entidades-chave da pergunta do usuario para buscar em um grafo "
        "de conhecimento. Retorne JSON no formato "
        '{"entities": [{"name": "nome canonico"}]}. '
        "Inclua apenas nomes de pessoas, projetos, organizacoes, lugares ou conceitos "
        "centrais. Nao explique a resposta."
    )


def _entity_name(item) -> str | None:  # noqa: ANN001
    if isinstance(item, str):
        value = item
    elif isinstance(item, dict):
        value = item.get("name") or item.get("entity")
    else:
        return None
    value = str(value).strip()
    return value or None


async def extract_query_entities(llm, query: str, max_entities: int) -> list[str]:  # noqa: ANN001
    if llm is None:
        return []

    data = await llm.complete_json(_system_prompt(max_entities), query)
    seen: set[str] = set()
    result: list[str] = []
    for item in data.get("entities", []):
        name = _entity_name(item)
        if name is None:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= max_entities:
            break
    return result
```

- [ ] **Step 4: Run query entity tests**

Run:

```bash
uv run pytest tests/test_query_entities.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit query entity fallback**

```bash
git add src/brain/extraction/query_entities.py tests/test_query_entities.py
git commit -m "feat(brain): extrai entidades de consultas"
```

## Task 3: Retriever Deep Search

**Files:**
- Modify: `src/brain/search/retriever.py`
- Test: `tests/integration/test_retriever.py`

- [ ] **Step 1: Add failing retriever tests**

Modify the import at the top of `tests/integration/test_retriever.py`:

```python
from brain.search.retriever import deep_search, search
```

Append these helper classes and tests to `tests/integration/test_retriever.py`:

```python
class FakeLLM:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"entities": []}
        self.error = error

    async def complete_json(self, system, user):
        if self.error is not None:
            raise self.error
        return self.payload


async def test_deep_search_combina_chunks_e_grafo_por_fast_path(session):
    doc = await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.11)})
    out = await deep_search(session, emb, None, "brain", limit=10, depth=1)

    assert out["query"] == "brain"
    assert out["results"][0]["id"] == str(doc.id)
    assert out["graph"]["relationships"] == [
        {"from": "Hermes", "to": "brain", "type": "curates", "seed": "brain", "depth": 1}
    ]
    assert out["meta"]["seed_strategy"] == "substring"


async def test_deep_search_usa_fallback_llm_quando_substring_nao_encontra_seed(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await session.commit()

    emb = FakeEmbedder({"Como o projeto se relaciona com o curador?": _vec(0.11)})
    llm = FakeLLM({"entities": [{"name": "brain"}]})
    out = await deep_search(
        session,
        emb,
        llm,
        "Como o projeto se relaciona com o curador?",
        limit=10,
        depth=1,
    )

    assert out["meta"]["seed_strategy"] == "llm"
    assert [rel["type"] for rel in out["graph"]["relationships"]] == ["curates"]


async def test_deep_search_sem_seeds_retorna_chunks_e_grafo_vazio(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta abstrata": _vec(0.11)})
    out = await deep_search(session, emb, FakeLLM({"entities": []}), "consulta abstrata")

    assert out["results"]
    assert out["graph"] == {"entities": [], "relationships": []}
    assert out["meta"]["seed_strategy"] == "none"


async def test_deep_search_fallback_llm_falha_retorna_warning(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await session.commit()

    emb = FakeEmbedder({"consulta abstrata": _vec(0.11)})
    out = await deep_search(
        session,
        emb,
        FakeLLM(error=RuntimeError("llm indisponivel")),
        "consulta abstrata",
    )

    assert out["results"]
    assert out["graph"] == {"entities": [], "relationships": []}
    assert out["meta"]["seed_strategy"] == "none"
    assert out["meta"]["warnings"] == ["query entity fallback failed: llm indisponivel"]
```

- [ ] **Step 2: Run retriever tests and confirm failure**

Run:

```bash
uv run pytest tests/integration/test_retriever.py -q
```

Expected: FAIL with `ImportError` or `AttributeError` for `deep_search`.

- [ ] **Step 3: Implement retriever orchestration**

Replace `src/brain/search/retriever.py` with:

```python
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
    seen: set[str] = set()
    result: list[dict] = []
    for entity in entities:
        name = (entity.get("name") or "").strip()
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


async def _resolve_seed_entities(session, query: str, namespace: str, max_entities: int) -> tuple[list[dict], str]:
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
    warnings: list[str] = []
    try:
        candidates = await extract_query_entities(llm, query, max_entities)
    except Exception as exc:  # noqa: BLE001
        return [], [f"query entity fallback failed: {exc}"]

    resolved: list[dict] = []
    for candidate in candidates:
        resolved.extend(await age.search_entities(session, candidate, namespace))
        if len(_dedupe_entities(resolved, max_entities)) >= max_entities:
            break
    return _dedupe_entities(resolved, max_entities), warnings


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
            if entity["name"] in seed_names and entity["depth"] == 0:
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
```

- [ ] **Step 4: Run retriever tests**

Run:

```bash
uv run pytest tests/integration/test_retriever.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit retriever orchestration**

```bash
git add src/brain/search/retriever.py tests/integration/test_retriever.py
git commit -m "feat(brain): orquestra deep search"
```

## Task 4: MCP Handler and Server Tool

**Files:**
- Modify: `src/brain/mcp/handlers.py`
- Modify: `src/brain/mcp/server.py`
- Test: `tests/integration/test_mcp_handlers.py`

- [ ] **Step 1: Add failing MCP tests**

Append these tests to `tests/integration/test_mcp_handlers.py`:

```python
async def test_deep_search_permite_client_e_retorna_grafo_estruturado(deps):
    await _as_curator(
        handlers.create_note,
        deps,
        "projetos/brain.md",
        "# Brain\n\nConhecimento curado de projeto.",
    )
    async with deps.session_factory() as s:
        await handlers.age.upsert_entity(s, "brain", "projeto", "curated")
        await handlers.age.upsert_entity(s, "Hermes", "agente", "curated")
        await handlers.age.upsert_relation(s, "Hermes", "brain", "curates", "curated")
        await s.commit()

    out = await _as_client(handlers.deep_search, deps, "brain", limit=10, depth=1)

    assert out["results"]
    assert out["graph"]["relationships"] == [
        {"from": "Hermes", "to": "brain", "type": "curates", "seed": "brain", "depth": 1}
    ]
    assert out["meta"]["depth"] == 1
    assert out["meta"]["max_entities"] == 3


@pytest.mark.parametrize("depth", [0, -1, 4, True, False, "2", 1.5])
async def test_deep_search_rejeita_depth_invalido(deps, depth):
    with pytest.raises(ValueError, match="depth"):
        await _as_client(handlers.deep_search, deps, "brain", depth=depth)


@pytest.mark.parametrize("max_entities", [0, -1, 4, True, False, "2", 1.5])
async def test_deep_search_rejeita_max_entities_invalido(deps, max_entities):
    with pytest.raises(ValueError, match="max_entities"):
        await _as_client(handlers.deep_search, deps, "brain", max_entities=max_entities)


async def test_deep_search_trata_rel_types_vazio_como_none(deps):
    out = await _as_client(handlers.deep_search, deps, "brain", rel_types=[])

    assert out["meta"]["rel_types"] is None


async def test_mcp_deep_search_schema_publico(deps):
    mcp = create_mcp_server(deps)
    tools = await mcp.list_tools()
    deep_tool = next(tool for tool in tools if tool.name == "deep_search")

    assert set(deep_tool.inputSchema["properties"]) == {
        "query",
        "limit",
        "depth",
        "max_entities",
        "rel_types",
        "filters",
        "namespace",
    }
    assert deep_tool.inputSchema["required"] == ["query"]
```

- [ ] **Step 2: Run MCP tests and confirm failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -q
```

Expected: FAIL with `AttributeError: module 'brain.mcp.handlers' has no attribute 'deep_search'`.

- [ ] **Step 3: Add handler import and bounded integer helper**

In `src/brain/mcp/handlers.py`, change the retriever import near the top to:

```python
from brain.search.retriever import deep_search as _deep_search
from brain.search.retriever import search as _search
```

Add this helper after `_require_client_or_curator`:

```python
def _bounded_int(value, *, name: str, min_value: int, max_value: int) -> int:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} deve ser um inteiro entre {min_value} e {max_value}")
    if value < min_value or value > max_value:
        raise ValueError(f"{name} deve ser entre {min_value} e {max_value}")
    return value
```

- [ ] **Step 4: Add `handlers.deep_search`**

In `src/brain/mcp/handlers.py`, add this function immediately after `search`:

```python
async def deep_search(
    deps: Deps,
    query: str,
    *,
    limit: int | None = 10,
    depth: int = 1,
    max_entities: int = 3,
    rel_types: list[str] | None = None,
    filters: dict | None = None,
    namespace: str = "curated",
) -> dict:
    _require_client_or_curator()
    resolved_limit = repo.normalize_search_limit(10 if limit is None else limit)
    resolved_depth = _bounded_int(depth, name="depth", min_value=1, max_value=3)
    resolved_max_entities = _bounded_int(
        max_entities,
        name="max_entities",
        min_value=1,
        max_value=3,
    )
    resolved_rel_types = rel_types or None

    async with deps.session_factory() as s:
        return await _deep_search(
            s,
            deps.embedder,
            deps.llm,
            query,
            limit=resolved_limit,
            depth=resolved_depth,
            max_entities=resolved_max_entities,
            rel_types=resolved_rel_types,
            filters=filters if isinstance(filters, dict) else None,
            namespace=namespace if isinstance(namespace, str) else "curated",
        )
```

- [ ] **Step 5: Register MCP tool**

In `src/brain/mcp/server.py`, add this tool immediately after the existing `search` tool:

```python
    @mcp.tool()
    async def deep_search(
        query: str,
        limit: int = 10,
        depth: int = 1,
        max_entities: int = 3,
        rel_types: list[str] | None = None,
        filters: dict | None = None,
        namespace: str = "curated",
    ) -> dict:
        return await handlers.deep_search(
            deps,
            query,
            limit=limit,
            depth=depth,
            max_entities=max_entities,
            rel_types=rel_types,
            filters=filters,
            namespace=namespace,
        )
```

- [ ] **Step 6: Run MCP tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit MCP exposure**

```bash
git add src/brain/mcp/handlers.py src/brain/mcp/server.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): expoe deep search no MCP"
```

## Task 5: Full Verification and Regression

**Files:**
- No planned code changes.

- [ ] **Step 1: Run focused test set**

Run:

```bash
uv run pytest tests/test_query_entities.py tests/integration/test_graph.py tests/integration/test_retriever.py tests/integration/test_mcp_handlers.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
uv run pytest
```

Expected: PASS. If integration tests require Docker and Docker is unavailable, record the exact failing command and error before stopping.

- [ ] **Step 3: Inspect public diff**

Run:

```bash
git diff --stat HEAD~4..HEAD
git log --oneline -n 5
```

Expected: the recent commits cover graph paths, query entity extraction, retriever orchestration, and MCP exposure. No unrelated files should be present.

- [ ] **Step 4: Final commit only if verification required fixes**

If Step 1 or Step 2 required small fixes, commit only those files:

```bash
git add src/brain/graph/age.py src/brain/extraction/query_entities.py src/brain/search/retriever.py src/brain/mcp/handlers.py src/brain/mcp/server.py tests/integration/test_graph.py tests/test_query_entities.py tests/integration/test_retriever.py tests/integration/test_mcp_handlers.py
git commit -m "test(brain): estabiliza deep search"
```

If no fixes were needed, do not create an empty commit.

## Self-Review

- Spec coverage: covered separate `deep_search`, unchanged `search`, graph paths, query entity fallback, retriever orchestration, handler guardrails, payload metadata, errors, and tests.
- Placeholder scan: no deferred sections, no unspecified test instructions, and no open implementation slots.
- Type consistency: public signature uses `query`, `limit`, `depth`, `max_entities`, `rel_types`, `filters`, `namespace`; internal graph signature uses `seeds`, `namespace`, `depth`, `rel_types`, `limit`.
