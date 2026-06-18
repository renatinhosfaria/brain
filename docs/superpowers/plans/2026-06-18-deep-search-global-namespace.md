# Deep Search Global Namespace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `deep_search` search the Knowledge Graph across all namespaces when `namespace` is omitted, while keeping textual results restricted to curated chunks and preserving curator-only write/admin operations.

**Architecture:** Keep the public `search` tool unchanged. Change `deep_search` so `namespace=None` means global graph search and explicit `namespace="..."` means single-namespace graph search. Carry `namespace` in every graph entity and relationship so a flat global result remains unambiguous.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, SQLAlchemy async, Apache AGE Cypher, pgvector, FastMCP.

---

## File Structure

- Modify: `src/brain/graph/age.py`  
  Add namespace-aware entity search and namespace-bearing relationship paths.

- Modify: `src/brain/search/retriever.py`  
  Treat omitted `namespace` as global graph search, dedupe seeds by `(name, namespace)`, and return namespace metadata.

- Modify: `src/brain/mcp/handlers.py`  
  Remove the client namespace block from `deep_search` and normalize empty namespace to `None`.

- Modify: `src/brain/mcp/server.py`  
  Change the MCP tool default from `"curated"` to `None`.

- Modify: `tests/integration/test_graph.py`  
  Add graph-layer tests for global entity search and namespace-bearing relationship paths.

- Modify: `tests/integration/test_retriever.py`  
  Add retriever tests for global graph search, explicit namespace scoping, and flat namespace-bearing payloads.

- Modify: `tests/integration/test_mcp_handlers.py`  
  Replace the old client namespace rejection test with tests that permit client global and explicit namespace graph reads.

- Modify: `README.md`  
  Document that clients can use `deep_search`; graph admin tools remain curator-only.

---

### Task 1: Graph Layer Namespace Payloads And Global Entity Search

**Files:**
- Modify: `tests/integration/test_graph.py`
- Modify: `src/brain/graph/age.py`

- [ ] **Step 1: Add failing graph tests**

Append these tests to `tests/integration/test_graph.py` after the existing `test_search_entities` test:

```python
async def test_search_entities_sem_namespace_retorna_todos_com_namespace(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await session.commit()

    found = await age.search_entities(session, "brain", None)

    assert found == [
        {"name": "brain", "type": "projeto", "namespace": "curated"},
        {"name": "brain", "type": "projeto", "namespace": "trabalho"},
    ]


async def test_search_entities_sem_namespace_respeita_limit_global(session):
    await age.upsert_entity(session, "Brain A", "projeto", "curated")
    await age.upsert_entity(session, "Brain B", "projeto", "trabalho")
    await age.upsert_entity(session, "Brain C", "projeto", "pessoal")
    await session.commit()

    found = await age.search_entities(session, "Brain", None, limit=2)

    assert len(found) == 2
    assert all("namespace" in entity for entity in found)
```

Append these tests after the existing `test_get_relationship_paths_retorna_entidades_relacoes_direcao_e_depth` test:

```python
async def test_get_relationship_paths_global_retorna_lista_unica_com_namespace(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")

    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        [
            {"name": "brain", "namespace": "curated"},
            {"name": "brain", "namespace": "trabalho"},
        ],
        None,
        depth=1,
    )

    assert out["relationships"] == [
        {
            "from": "Hermes",
            "to": "brain",
            "type": "curates",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        },
        {
            "from": "Renato",
            "to": "brain",
            "type": "owns",
            "namespace": "trabalho",
            "seed": "brain",
            "depth": 1,
        },
    ]
    assert {
        (entity["name"], entity["namespace"])
        for entity in out["entities"]
    } == {
        ("brain", "curated"),
        ("Hermes", "curated"),
        ("brain", "trabalho"),
        ("Renato", "trabalho"),
    }


async def test_get_relationship_paths_namespace_expresso_descarta_seed_de_outro_namespace(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")

    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        [
            {"name": "brain", "namespace": "curated"},
            {"name": "brain", "namespace": "trabalho"},
        ],
        "curated",
        depth=1,
    )

    assert out["relationships"] == [
        {
            "from": "Hermes",
            "to": "brain",
            "type": "curates",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        }
    ]
    assert {entity["namespace"] for entity in out["entities"]} == {"curated"}
```

- [ ] **Step 2: Run graph tests and confirm failure**

Run:

```bash
uv run pytest tests/integration/test_graph.py -q
```

Expected: FAIL because `search_entities(..., None)` still emits `namespace = null` in Cypher or returns no `namespace`, and `get_relationship_paths` still expects `list[str]` plus a required namespace.

- [ ] **Step 3: Add namespace to entity payloads**

In `src/brain/graph/age.py`, replace `_entity_payload` with:

```python
def _entity_payload(node: dict, *, seed: str, depth: int) -> dict:
    props = _props(node)
    return {
        "name": props.get("name"),
        "type": props.get("type"),
        "namespace": props.get("namespace"),
        "seed": seed,
        "depth": depth,
    }
```

- [ ] **Step 4: Replace `search_entities` with namespace-optional search**

In `src/brain/graph/age.py`, replace the full `search_entities` function with:

```python
async def search_entities(
    session: AsyncSession,
    query: str,
    namespace: str | None,
    limit: int | None = None,
) -> list[dict]:
    await _prepare(session)
    limit_clause = ""
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit deve ser um inteiro positivo")
        limit_clause = f"LIMIT {limit} "

    namespace_match = (
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        if namespace is not None
        else "MATCH (n:Entity) "
    )
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"{namespace_match}"
        f"WHERE toLower(n.name) CONTAINS toLower({_lit(query)}) "
        f"RETURN n.name, n.type, n.namespace "
        f"ORDER BY toLower(n.name), n.namespace, n.name, n.type "
        f"{limit_clause}$cy$) AS (name agtype, type agtype, namespace agtype)"
    )
    rows = (await session.execute(text(q))).all()
    return [
        {"name": _unwrap(n), "type": _unwrap(t), "namespace": _unwrap(ns)}
        for n, t, ns in rows
    ]
```

- [ ] **Step 5: Add seed normalization helpers**

In `src/brain/graph/age.py`, add these helpers after `_dedupe_strings`:

```python
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
            seed_namespace = str(seed_namespace).strip() if seed_namespace is not None else ""
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
```

- [ ] **Step 6: Update `get_relationship_paths` signature and seed loop**

In `src/brain/graph/age.py`, update the function signature and the local seed variables at the top of `get_relationship_paths`:

```python
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
    relationship_entities: dict[tuple[str, str, str, str, str, int], tuple[dict, dict]] = {}

    allowed_types_literal = _lit(sorted(allowed_types))
    should_stop = False

    for seed_entry in seed_entries:
        seed = seed_entry["name"]
        seed_namespace = seed_entry["namespace"]
```

Inside the same function, replace namespace uses in the Cypher pattern:

```python
parts = [
    f"(s:Entity {{name: {_lit(seed)}, namespace: {_lit(seed_namespace)}}})",
]
for idx, edge in enumerate(edge_vars):
    target = "n" if idx == path_depth - 1 else intermediate_nodes[idx]
    parts.append(f"-[{edge}:REL]-({target}:Entity {{namespace: {_lit(seed_namespace)}}})")
```

Replace the namespace check after parsing nodes:

```python
if any(_props(node).get("namespace") != seed_namespace for node in nodes):
    continue
```

- [ ] **Step 7: Add namespace to relationship keys and payloads**

In `src/brain/graph/age.py`, inside the `for hop, from_name, ... in parsed_rels` loop, replace the relationship payload and key with:

```python
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
```

Near the end of `get_relationship_paths`, replace sorting, `rel_key`, and entity dedupe with:

```python
relationships.sort(key=lambda r: (r["namespace"], r["seed"], r["depth"], r["from"], r["to"], r["type"]))
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
```

- [ ] **Step 8: Update existing graph test expectations for namespace**

In `tests/integration/test_graph.py`, update existing exact relationship/entity assertions to include `namespace` where full dict equality is used. For example:

```python
assert {"name": "brain", "type": "projeto", "namespace": "curated", "seed": "brain", "depth": 0} in out["entities"]
```

and:

```python
assert out["relationships"] == [
    {
        "from": "brain",
        "to": "Vault",
        "type": "stores",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    }
]
```

- [ ] **Step 9: Run graph tests**

Run:

```bash
uv run pytest tests/integration/test_graph.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit graph layer**

```bash
git add src/brain/graph/age.py tests/integration/test_graph.py
git commit -m "feat(brain): suporte namespace global no grafo"
```

---

### Task 2: Retriever Global Namespace Orchestration

**Files:**
- Modify: `tests/integration/test_retriever.py`
- Modify: `src/brain/search/retriever.py`

- [ ] **Step 1: Add failing retriever tests**

Append these tests to `tests/integration/test_retriever.py` after `test_deep_search_namespace_controla_so_grafo`:

```python
async def test_deep_search_sem_namespace_busca_grafo_global_em_lista_unica(session):
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

    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.11)})
    out = await deep_search(session, emb, None, "brain", namespace=None, depth=1, max_entities=3)

    assert out["results"][0]["id"] == str(doc.id)
    assert {result["namespace"] for result in out["results"]} == {"curated"}
    assert out["graph"]["relationships"] == [
        {
            "from": "Hermes",
            "to": "brain",
            "type": "curates",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        },
        {
            "from": "Renato",
            "to": "brain",
            "type": "owns",
            "namespace": "trabalho",
            "seed": "brain",
            "depth": 1,
        },
    ]
    assert out["meta"]["namespace_strategy"] == "all"
    assert out["meta"]["namespaces"] == ["curated", "trabalho"]


async def test_deep_search_namespace_explicito_limita_apenas_grafo(session):
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

    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.11)})
    out = await deep_search(session, emb, None, "brain", namespace="trabalho", depth=1)

    assert out["results"][0]["id"] == str(doc.id)
    assert {result["namespace"] for result in out["results"]} == {"curated"}
    assert out["graph"]["relationships"] == [
        {
            "from": "Renato",
            "to": "brain",
            "type": "owns",
            "namespace": "trabalho",
            "seed": "brain",
            "depth": 1,
        }
    ]
    assert out["meta"]["namespace_strategy"] == "single"
    assert out["meta"]["namespaces"] == ["trabalho"]


async def test_deep_search_fallback_llm_global_resolve_seed_em_qualquer_namespace(session):
    await _add_document_chunk(
        session,
        namespace="curated",
        repo_path="projetos/brain.md",
        text="nota curada sobre brain",
        seed=0.10,
    )
    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    emb = FakeEmbedder({"Como o projeto se relaciona com o dono?": _vec(0.11)})
    llm = FakeLLM({"entities": [{"name": "brain"}]})
    out = await deep_search(
        session,
        emb,
        llm,
        "Como o projeto se relaciona com o dono?",
        namespace=None,
    )

    assert out["meta"]["seed_strategy"] == "llm"
    assert out["meta"]["namespace_strategy"] == "all"
    assert out["meta"]["namespaces"] == ["trabalho"]
    assert out["graph"]["relationships"] == [
        {
            "from": "Renato",
            "to": "brain",
            "type": "owns",
            "namespace": "trabalho",
            "seed": "brain",
            "depth": 1,
        }
    ]
```

- [ ] **Step 2: Run retriever tests and confirm failure**

Run:

```bash
uv run pytest tests/integration/test_retriever.py -q
```

Expected: FAIL because `deep_search` defaults to `"curated"`, seed dedupe ignores namespace, and graph metadata lacks `namespace_strategy` and `namespaces`.

- [ ] **Step 3: Replace entity dedupe with namespace-aware dedupe**

In `src/brain/search/retriever.py`, replace `_dedupe_entities` with:

```python
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
```

- [ ] **Step 4: Update seed resolution signatures**

In `src/brain/search/retriever.py`, update `_resolve_seed_entities` and `_resolve_llm_entities` signatures from `namespace: str` to:

```python
namespace: str | None
```

Keep the calls to `age.search_entities(session, query, namespace, limit=max_entities)` and `age.search_entities(session, candidate, namespace, limit=remaining)` unchanged after the type change.

- [ ] **Step 5: Add namespace metadata helper**

In `src/brain/search/retriever.py`, add this helper after `_valid_max_entities`:

```python
def _graph_namespaces(graph: dict) -> list[str]:
    namespaces = {
        str(item.get("namespace"))
        for collection in (graph.get("entities", []), graph.get("relationships", []))
        for item in collection
        if item.get("namespace")
    }
    return sorted(namespaces)
```

- [ ] **Step 6: Update `deep_search` signature and metadata returns**

In `src/brain/search/retriever.py`, change the `deep_search` signature to:

```python
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
) -> dict:
```

Immediately after `resolved_max_entities = _valid_max_entities(max_entities)`, add:

```python
namespace_strategy = "all" if namespace is None else "single"
```

In every early return `meta` dict inside `deep_search`, add:

```python
"namespace_strategy": namespace_strategy,
"namespaces": [],
```

- [ ] **Step 7: Pass namespace-aware seeds to graph traversal**

In `src/brain/search/retriever.py`, replace the graph traversal block with:

```python
graph = {"entities": [], "relationships": []}
if seeds:
    graph = await age.get_relationship_paths(
        session,
        seeds,
        namespace,
        depth=depth,
        rel_types=rel_types,
        limit=50,
    )
    seed_keys = {(seed["name"], seed["namespace"]) for seed in seeds}
    for entity in graph["entities"]:
        if (
            entity["depth"] == 0
            and (entity["name"], entity.get("namespace")) in seed_keys
        ):
            entity["matched_by"] = seed_strategy
        else:
            entity["matched_by"] = "relationship"
namespaces = _graph_namespaces(graph)
```

In the final returned `meta`, add:

```python
"namespace_strategy": namespace_strategy,
"namespaces": namespaces,
```

- [ ] **Step 8: Update existing retriever expectations for namespace**

In `tests/integration/test_retriever.py`, update exact relationship assertions to include `namespace`. For example:

```python
assert out["graph"]["relationships"] == [
    {
        "from": "Hermes",
        "to": "brain",
        "type": "curates",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    }
]
```

Also update tests that inspect matched seed entities so they do not assume names alone are unique across namespaces:

```python
matched_seed_keys = {
    (entity["name"], entity["namespace"])
    for entity in out["graph"]["entities"]
    if entity.get("matched_by") == "substring"
}
```

- [ ] **Step 9: Run retriever tests**

Run:

```bash
uv run pytest tests/integration/test_retriever.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit retriever layer**

```bash
git add src/brain/search/retriever.py tests/integration/test_retriever.py
git commit -m "feat(brain): busca grafo global no deep search"
```

---

### Task 3: MCP Contract And Permissions

**Files:**
- Modify: `tests/integration/test_mcp_handlers.py`
- Modify: `src/brain/mcp/handlers.py`
- Modify: `src/brain/mcp/server.py`

- [ ] **Step 1: Replace the client namespace rejection test**

In `tests/integration/test_mcp_handlers.py`, replace `test_deep_search_rejeita_namespace_customizado_para_client` with:

```python
async def test_deep_search_permite_namespace_customizado_para_client(deps):
    async with deps.session_factory() as s:
        doc = await repo.upsert_document(
            s,
            namespace="curated",
            repo_path="projetos/brain-client-tenant.md",
            title=None,
            raw_content="nota curada sobre brain",
            content_hash="deep-search-client-tenant",
            commit_sha=None,
        )
        await repo.replace_chunks(
            s,
            doc.id,
            [{"ordinal": 0, "text": "nota curada sobre brain", "token_count": 1}],
            [[0.2] * 2000],
        )
        await handlers.age.upsert_entity(s, "brain", "projeto", "tenant-b")
        await handlers.age.upsert_entity(s, "Hermes", "agente", "tenant-b")
        await handlers.age.upsert_relation(s, "Hermes", "brain", "curates", "tenant-b")
        await s.commit()

    out = await _as_client(handlers.deep_search, deps, "brain", namespace="tenant-b")

    assert out["results"][0]["id"] == str(doc.id)
    assert out["graph"]["relationships"] == [
        {
            "from": "Hermes",
            "to": "brain",
            "type": "curates",
            "namespace": "tenant-b",
            "seed": "brain",
            "depth": 1,
        }
    ]
    assert out["meta"]["namespace_strategy"] == "single"
    assert out["meta"]["namespaces"] == ["tenant-b"]
```

Append this test after `test_deep_search_permite_namespace_customizado_para_curator`:

```python
async def test_deep_search_sem_namespace_para_client_busca_grafo_global(deps):
    async with deps.session_factory() as s:
        doc = await repo.upsert_document(
            s,
            namespace="curated",
            repo_path="projetos/brain-global.md",
            title=None,
            raw_content="nota curada sobre brain",
            content_hash="deep-search-global",
            commit_sha=None,
        )
        await repo.replace_chunks(
            s,
            doc.id,
            [{"ordinal": 0, "text": "nota curada sobre brain", "token_count": 1}],
            [[0.2] * 2000],
        )
        await handlers.age.upsert_entity(s, "brain", "projeto", "curated")
        await handlers.age.upsert_entity(s, "Hermes", "agente", "curated")
        await handlers.age.upsert_relation(s, "Hermes", "brain", "curates", "curated")
        await handlers.age.upsert_entity(s, "brain", "projeto", "tenant-b")
        await handlers.age.upsert_entity(s, "Renato", "pessoa", "tenant-b")
        await handlers.age.upsert_relation(s, "Renato", "brain", "owns", "tenant-b")
        await s.commit()

    out = await _as_client(handlers.deep_search, deps, "brain")

    assert out["results"][0]["id"] == str(doc.id)
    assert out["meta"]["namespace_strategy"] == "all"
    assert out["meta"]["namespaces"] == ["curated", "tenant-b"]
    assert {rel["namespace"] for rel in out["graph"]["relationships"]} == {"curated", "tenant-b"}
```

- [ ] **Step 2: Update MCP schema test expectation**

In `tests/integration/test_mcp_handlers.py`, keep the `namespace` property in `test_mcp_deep_search_public_schema`. No code change is needed for the property set, but after implementation this test should still pass with `namespace` optional.

- [ ] **Step 3: Run MCP handler tests and confirm failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -q
```

Expected: FAIL because the handler still blocks client explicit namespace and the MCP tool still defaults `namespace` to `"curated"`.

- [ ] **Step 4: Update handler namespace normalization and permission**

In `src/brain/mcp/handlers.py`, replace this block inside `deep_search`:

```python
resolved_rel_types = _normalize_rel_types(rel_types)
resolved_namespace = namespace if isinstance(namespace, str) else "curated"
if principal.type == "client" and resolved_namespace != "curated":
    raise PermissionError("curator required for non-curated namespace")
```

with:

```python
resolved_rel_types = _normalize_rel_types(rel_types)
if isinstance(namespace, str):
    resolved_namespace = namespace.strip() or None
else:
    resolved_namespace = None
```

Keep this line at the top of the handler so invalid principals are still rejected:

```python
principal = _require_deep_search_principal()
```

- [ ] **Step 5: Update MCP server default**

In `src/brain/mcp/server.py`, change the `deep_search` tool signature from:

```python
namespace: str = "curated",
```

to:

```python
namespace: str | None = None,
```

- [ ] **Step 6: Update existing MCP exact relationship expectations**

In `tests/integration/test_mcp_handlers.py`, update exact relationship assertions under deep search tests to include `namespace`. Example:

```python
assert out["graph"]["relationships"] == [
    {
        "from": "Hermes",
        "to": "brain",
        "type": "curates",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    }
]
```

- [ ] **Step 7: Run MCP handler tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit MCP contract**

```bash
git add src/brain/mcp/handlers.py src/brain/mcp/server.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): permite deep search global para clientes"
```

---

### Task 4: Documentation And Public Permission Matrix

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update client tool list**

In `README.md`, replace the client access list under `## Ferramentas MCP de inbox e curadoria` with:

```markdown
Clientes de agente têm acesso limitado:
- `search`: busca semântica apenas em notas curadas.
- `deep_search`: busca semântica em notas curadas com contexto relacional do
  grafo. Quando `namespace` é omitido, consulta o grafo em todos os namespaces;
  quando informado, limita apenas o grafo àquele namespace.
- `get_note`: lê uma nota curada por id ou caminho.
- `submit_agent_note`: envia uma nota bruta em Markdown ou `messages`.
```

- [ ] **Step 2: Clarify curator-only graph admin tools**

In `README.md`, replace the paragraph that starts with `Esta lista destaca o fluxo de inbox e curadoria.` with:

```markdown
Esta lista destaca o fluxo de inbox e curadoria. Ferramentas técnicas de
documentos, reindexação e administração do grafo, como `get_document`,
`list_documents`, `reindex`, `get_entity`, `search_entities`, `get_related`,
`update_entity`, `merge_entities` e `delete_entity`, continuam expostas apenas
ao curador para compatibilidade e operações. Clientes consultam o grafo pela
interface estruturada de leitura do `deep_search`, não por essas ferramentas
administrativas.
```

- [ ] **Step 3: Run doc diff check**

Run:

```bash
git diff -- README.md
```

Expected: diff shows only the client tool list and curator-only graph admin clarification.

- [ ] **Step 4: Commit docs**

```bash
git add README.md
git commit -m "docs(brain): documenta deep search global para clientes"
```

---

### Task 5: Final Verification

**Files:**
- Verify only; no file edits expected.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/integration/test_graph.py tests/integration/test_retriever.py tests/integration/test_mcp_handlers.py -q
```

Expected: PASS.

- [ ] **Step 2: Run unit tests touched by query fallback**

Run:

```bash
uv run pytest tests/test_query_entities.py tests/test_config.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS. If integration tests require the local `brain-postgres:local` image and it is missing, build it with:

```bash
docker build -t brain-postgres:local docker/postgres
uv run pytest -q
```

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: clean worktree after commits; recent commits include the graph, retriever, MCP, and docs commits from this plan.

---

## Self-Review

- Spec coverage: Covered `namespace=None` global graph search, explicit namespace scoping, flat graph payload with `namespace`, client access to global and explicit namespace graph reads, curated-only textual results, curator-only write/admin operations, handler validation, tests, docs, and compatibility.
- Placeholder scan: No `TBD`, no `TODO`, no open-ended "add tests" instructions, and all code-changing steps include concrete code blocks.
- Type consistency: Public `deep_search` uses `namespace: str | None = None`; graph `search_entities` accepts `namespace: str | None`; graph path traversal accepts namespace-aware seed dicts while retaining compatibility for string seeds with explicit namespace.
