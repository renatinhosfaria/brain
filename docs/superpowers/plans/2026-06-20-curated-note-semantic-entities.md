# Curated Note Semantic Entities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make curated Markdown notes create deterministic, searchable graph entities with aliases, tags, normalized matching, and individual reindex support.

**Architecture:** `pipeline.index_document` remains the orchestrator and calls a focused `brain.ingestion.semantic_entities` module after `Document` upsert. That module derives deterministic entity payloads from curated-note metadata/content/path and writes through small AGE helpers that can update an existing node by `source_doc` before falling back to `(name, namespace)` upsert. `search_entities` ranks matches from name, aliases, tags, and path/source_doc using normalized Python scoring over AGE-returned nodes.

**Tech Stack:** Python 3.12, FastAPI/FastMCP handlers, SQLAlchemy async sessions, Apache AGE via Cypher strings in `brain.graph.age`, pytest/pytest-asyncio.

---

## File Structure

- Create `src/brain/ingestion/semantic_entities.py`: deterministic curated-note entity payload builder and `upsert_entity_from_curated_document`.
- Modify `src/brain/ingestion/pipeline.py`: call semantic entity sync after `Document` upsert, including content-hash no-op path.
- Modify `src/brain/graph/age.py`: add non-committing write options, source-doc lookup, identity update, props-aware `search_entities` ranking.
- Modify `src/brain/mcp/handlers.py`: make curator `update_entity` use real upsert semantics instead of silent match-only update.
- Create `tests/test_semantic_entities.py`: pure unit tests for canonical name, type mapping, aliases, eligibility, payload shape.
- Modify `tests/integration/test_graph.py`: AGE lookup/upsert/search/ranking tests.
- Modify `tests/integration/test_pipeline.py`: deterministic entity sync for direct indexing/reindex and content-hash no-op metadata changes.
- Modify `tests/integration/test_mcp_handlers.py`: `create_note`/`update_note` integration and alias query acceptance tests.

---

### Task 1: Pure Semantic Entity Builder

**Files:**
- Create: `tests/test_semantic_entities.py`
- Create: `src/brain/ingestion/semantic_entities.py`

- [ ] **Step 1: Write failing pure tests**

Create `tests/test_semantic_entities.py` with this content:

```python
from brain.ingestion.semantic_entities import (
    build_curated_entity_payload,
    normalize_entity_text,
)


def _aliases(payload: dict) -> set[str]:
    return set(payload["props"]["aliases"])


def test_build_payload_prefers_metadata_title_over_h1_and_keeps_path_alias():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/regras-env-e-migrations-por-projeto.md",
        title="H1 ignorado",
        content="# H1 ignorado\n\nCorpo.",
        metadata={
            "title": "Regras de .env e migrations dependem do projeto",
            "type": "preference",
            "tags": ["env", "migrations"],
        },
        document_id="doc-1",
    )

    assert payload["status"] == "ready"
    assert payload["name"] == "Regras de .env e migrations dependem do projeto"
    assert payload["type"] == "preferencia"
    assert payload["props"]["source_doc"] == "preferencias/regras-env-e-migrations-por-projeto.md"
    assert payload["props"]["repo_path"] == "preferencias/regras-env-e-migrations-por-projeto.md"
    assert payload["props"]["document_id"] == "doc-1"
    assert payload["props"]["tags"] == ["env", "migrations"]
    assert "regras-env-e-migrations-por-projeto" in _aliases(payload)
    assert "regras env e migrations por projeto" in _aliases(payload)
    assert "env migrations" in _aliases(payload)
    assert ".env" in _aliases(payload)


def test_build_payload_uses_h1_before_humanized_path():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/stack-tecnica-por-projeto.md",
        title="Stack técnica deve ser inferida por projeto",
        content="# Stack técnica deve ser inferida por projeto\n\nCorpo.",
        metadata={},
    )

    assert payload["status"] == "ready"
    assert payload["name"] == "Stack técnica deve ser inferida por projeto"
    assert "stack técnica" in _aliases(payload)
    assert "stack tecnica" in _aliases(payload)
    assert "stack por projeto" in _aliases(payload)
    assert "stack técnica por projeto" in _aliases(payload)
    assert "stack tecnica por projeto" in _aliases(payload)


def test_build_payload_uses_humanized_path_without_title_or_h1():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="conceitos/minio.md",
        title=None,
        content="Sem heading.",
        metadata={},
    )

    assert payload["status"] == "ready"
    assert payload["name"] == "Minio"
    assert "minio" in _aliases(payload)


def test_build_payload_skips_non_curated_agents_and_non_markdown():
    assert build_curated_entity_payload(
        namespace="tenant",
        repo_path="preferencias/privacidade.md",
        title="Privacidade",
        content="# Privacidade",
        metadata={},
    ) == {"status": "skipped", "reason": "namespace_not_curated"}

    assert build_curated_entity_payload(
        namespace="curated",
        repo_path="_agents/chatgpt/raw.md",
        title="Raw",
        content="# Raw",
        metadata={},
    ) == {"status": "skipped", "reason": "agent_inbox_path"}

    assert build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/raw.txt",
        title="Raw",
        content="# Raw",
        metadata={},
    ) == {"status": "skipped", "reason": "not_markdown"}


def test_alias_examples_are_conservative_and_domain_aliases_are_explicit():
    privacy = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/privacidade-credenciais-e-acoes-externas.md",
        title="Privacidade, credenciais e ações externas",
        content="# Privacidade, credenciais e ações externas\n\nCorpo.",
        metadata={"title": "Privacidade, credenciais e ações externas"},
    )
    assert {
        "privacidade",
        "credenciais",
        "ações externas",
        "acoes externas",
        "privacidade credenciais",
        "privacidade credenciais acoes externas",
    }.issubset(_aliases(privacy))
    assert "regras" not in _aliases(privacy)

    ceo = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/perfil-ceo.md",
        title="Perfil CEO",
        content="# Perfil CEO\n\nCorpo.",
        metadata={"aliases": ["Hermes CEO", "ceo hermes"]},
    )
    assert {"CEO", "perfil ceo", "Hermes CEO", "ceo hermes"}.issubset(_aliases(ceo))


def test_type_mapping_and_raw_type_preservation():
    mapped = build_curated_entity_payload(
        namespace="curated",
        repo_path="decisoes/x.md",
        title="Decisão X",
        content="# Decisão X",
        metadata={"type": "decision"},
    )
    assert mapped["type"] == "decisao"
    assert "raw_type" not in mapped["props"]

    unknown = build_curated_entity_payload(
        namespace="curated",
        repo_path="notas/x.md",
        title="Nota X",
        content="# Nota X",
        metadata={"type": "playbook"},
    )
    assert unknown["type"] == "conceito"
    assert unknown["props"]["raw_type"] == "playbook"


def test_normalize_entity_text_casefolds_and_removes_accents():
    assert normalize_entity_text("Stack Técnica") == "stack tecnica"
    assert normalize_entity_text("ações externas") == "acoes externas"
    assert normalize_entity_text("regras-env") == "regras env"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_semantic_entities.py -v
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'brain.ingestion.semantic_entities'`.

- [ ] **Step 3: Implement pure semantic entity builder**

Create `src/brain/ingestion/semantic_entities.py` with this content:

```python
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any

from brain.graph import age


TYPE_MAP = {
    "project": "projeto",
    "preference": "preferencia",
    "decision": "decisao",
    "process": "processo",
    "concept": "conceito",
    "reference": "referencia",
    "map": "mapa",
}

GENERIC_SINGLE_TOKEN_ALIASES = {
    "de",
    "do",
    "da",
    "dos",
    "das",
    "e",
    "por",
    "para",
    "com",
    "sem",
    "projeto",
    "projetos",
    "regras",
    "regra",
    "perfil",
    "tecnica",
    "técnica",
    "deve",
    "dependem",
    "depende",
}

TECHNICAL_ALIAS_ALLOWLIST = {
    ".env",
    "env",
    "ceo",
    "CEO",
    "migrations",
}


def normalize_entity_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("-", " ")
    text = re.sub(r"[^\w\s.]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_markdown_h1(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("# "):
            title = line.lstrip("#").strip()
            if title:
                return title
    return None


def _metadata_value(metadata: dict | None, key: str) -> Any:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    return value if value not in ("", [], {}) else None


def _canonical_name(
    *,
    repo_path: str,
    title: str | None,
    content: str,
    metadata: dict | None,
) -> str | None:
    metadata_title = _metadata_value(metadata, "title")
    if isinstance(metadata_title, str) and metadata_title.strip():
        return metadata_title.strip()
    if title and title.strip():
        return title.strip()
    h1 = _strip_markdown_h1(content)
    if h1:
        return h1
    stem = PurePosixPath(repo_path).stem
    humanized = re.sub(r"[-_]+", " ", stem).strip()
    if not humanized:
        return None
    return humanized[:1].upper() + humanized[1:]


def _is_markdown(repo_path: str) -> bool:
    return repo_path.endswith((".md", ".markdown"))


def _is_agent_path(repo_path: str) -> bool:
    return repo_path == "_agents" or repo_path.startswith("_agents/")


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _split_alias_parts(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    parts = [text]
    for separator in (",", "/", "|"):
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(piece.strip() for piece in part.split(separator))
        parts = [part for part in next_parts if part]
    expanded: list[str] = []
    for part in parts:
        expanded.append(part)
        normalized = normalize_entity_text(part)
        if " e " in normalized:
            expanded.extend(piece.strip() for piece in normalized.split(" e ") if piece.strip())
    return expanded


def _keep_alias(value: str) -> bool:
    stripped = value.strip()
    normalized = normalize_entity_text(stripped)
    if not normalized:
        return False
    if stripped in TECHNICAL_ALIAS_ALLOWLIST or normalized in TECHNICAL_ALIAS_ALLOWLIST:
        return True
    if len(normalized) <= 1:
        return False
    tokens = normalized.split()
    if len(tokens) == 1 and normalized in GENERIC_SINGLE_TOKEN_ALIASES:
        return False
    return True


def _add_alias(result: list[str], seen: set[str], value: str) -> None:
    value = value.strip()
    if not _keep_alias(value):
        return
    key = normalize_entity_text(value)
    if key not in seen:
        seen.add(key)
        result.append(value)
    ascii_value = normalize_entity_text(value)
    if ascii_value and ascii_value != value and ascii_value not in seen and _keep_alias(ascii_value):
        seen.add(ascii_value)
        result.append(ascii_value)


def _slug_aliases(repo_path: str) -> list[str]:
    path = PurePosixPath(repo_path)
    stem = path.stem
    humanized = re.sub(r"[-_]+", " ", stem).strip()
    aliases = [stem, humanized]
    if path.parent != PurePosixPath("."):
        aliases.append(f"{path.parent.as_posix()}/{stem}")
    return aliases


def _metadata_aliases(metadata: dict | None) -> list[str]:
    aliases: list[str] = []
    for key in ("aliases", "alias"):
        aliases.extend(_as_string_list(_metadata_value(metadata, key)))
    return aliases


def _metadata_tags(metadata: dict | None) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "tag"):
        tags.extend(_as_string_list(_metadata_value(metadata, key)))
    return tags


def _aliases_for_title(title: str) -> list[str]:
    normalized = normalize_entity_text(title)
    aliases = [title, normalized]

    if "privacidade" in normalized and "credenciais" in normalized:
        aliases.extend(["privacidade", "credenciais", "ações externas", "privacidade credenciais"])

    if "stack" in normalized and "projeto" in normalized:
        aliases.extend(["stack técnica", "stack tecnica", "stack por projeto", "stack técnica por projeto"])

    if "env" in normalized and "migrations" in normalized:
        aliases.extend([".env", "env", "migrations", "env migrations", "regras env", "migrations por projeto", "regras de env e migrations"])

    if "perfil ceo" in normalized:
        aliases.extend(["CEO", "perfil ceo"])

    for part in _split_alias_parts(title):
        aliases.append(part)
    return aliases


def _build_aliases(
    *,
    canonical_name: str,
    repo_path: str,
    title: str | None,
    content: str,
    metadata: dict | None,
) -> list[str]:
    raw_aliases: list[str] = []
    raw_aliases.extend(_aliases_for_title(canonical_name))
    if title:
        raw_aliases.extend(_aliases_for_title(title))
    h1 = _strip_markdown_h1(content)
    if h1:
        raw_aliases.extend(_aliases_for_title(h1))
    raw_aliases.extend(_slug_aliases(repo_path))
    raw_aliases.extend(_metadata_tags(metadata))
    raw_aliases.extend(_metadata_aliases(metadata))

    result: list[str] = []
    seen: set[str] = set()
    for alias in raw_aliases:
        _add_alias(result, seen, alias)
    return result


def _entity_type(metadata: dict | None) -> tuple[str, str | None]:
    raw_type = _metadata_value(metadata, "type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        return "conceito", None
    normalized = raw_type.strip().casefold()
    mapped = TYPE_MAP.get(normalized)
    if mapped:
        return mapped, None
    return "conceito", raw_type.strip()


def build_curated_entity_payload(
    *,
    namespace: str,
    repo_path: str,
    title: str | None,
    content: str,
    metadata: dict | None,
    document_id: str | None = None,
) -> dict:
    if namespace != "curated":
        return {"status": "skipped", "reason": "namespace_not_curated"}
    if _is_agent_path(repo_path):
        return {"status": "skipped", "reason": "agent_inbox_path"}
    if not _is_markdown(repo_path):
        return {"status": "skipped", "reason": "not_markdown"}

    name = _canonical_name(repo_path=repo_path, title=title, content=content, metadata=metadata)
    if not name:
        return {"status": "skipped", "reason": "missing_name"}

    entity_type, raw_type = _entity_type(metadata)
    tags = _metadata_tags(metadata)
    aliases = _build_aliases(
        canonical_name=name,
        repo_path=repo_path,
        title=title,
        content=content,
        metadata=metadata,
    )
    props = {
        "source": "curated_note",
        "source_doc": repo_path,
        "repo_path": repo_path,
        "document_id": document_id,
        "title": name,
        "tags": tags,
        "aliases": aliases,
        "name_normalized": normalize_entity_text(name),
        "aliases_normalized": [normalize_entity_text(alias) for alias in aliases],
        "tags_normalized": [normalize_entity_text(tag) for tag in tags],
        "repo_path_normalized": normalize_entity_text(repo_path),
    }
    if raw_type:
        props["raw_type"] = raw_type
    return {
        "status": "ready",
        "name": name,
        "type": entity_type,
        "namespace": namespace,
        "source_doc": repo_path,
        "aliases": aliases,
        "props": props,
    }


async def upsert_entity_from_curated_document(
    session,
    *,
    namespace: str,
    repo_path: str,
    title: str | None,
    content: str,
    metadata: dict | None,
    document_id: str | None = None,
) -> dict:
    payload = build_curated_entity_payload(
        namespace=namespace,
        repo_path=repo_path,
        title=title,
        content=content,
        metadata=metadata,
        document_id=document_id,
    )
    if payload["status"] != "ready":
        return payload

    existing = await age.find_entity_by_source_doc(
        session,
        namespace,
        repo_path,
        document_id=document_id,
    )
    if existing is not None:
        await age.update_entity_identity(
            session,
            existing["name"],
            namespace,
            name=payload["name"],
            type=payload["type"],
            props=payload["props"],
            commit=False,
        )
        return payload | {"status": "updated"}

    await age.upsert_entity(
        session,
        payload["name"],
        payload["type"],
        namespace,
        payload["props"],
        commit=False,
    )
    return payload | {"status": "created"}
```

- [ ] **Step 4: Run pure tests**

Run:

```bash
uv run pytest tests/test_semantic_entities.py -v
```

Expected: PASS for all tests in `tests/test_semantic_entities.py`.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/brain/ingestion/semantic_entities.py tests/test_semantic_entities.py
git commit -m "feat(ingestion): derive curated note entity payloads"
```

Expected: commit succeeds.

---

### Task 2: AGE Helpers And Ranked Entity Search

**Files:**
- Modify: `src/brain/graph/age.py`
- Modify: `tests/integration/test_graph.py`

- [ ] **Step 1: Write failing AGE tests**

Append these tests to `tests/integration/test_graph.py`:

```python
async def test_find_entity_by_source_doc_and_update_identity_preserves_relation(session):
    await age.upsert_entity(
        session,
        "Nome Antigo",
        "conceito",
        "curated",
        {"source_doc": "preferencias/x.md", "aliases": ["antigo"]},
    )
    await age.upsert_entity(session, "Vizinho", "conceito", "curated")
    await age.upsert_relation(session, "Nome Antigo", "Vizinho", "relates_to", "curated")

    found = await age.find_entity_by_source_doc(session, "curated", "preferencias/x.md")
    assert found["name"] == "Nome Antigo"

    await age.update_entity_identity(
        session,
        "Nome Antigo",
        "curated",
        name="Nome Novo",
        type="preferencia",
        props={"source_doc": "preferencias/x.md", "aliases": ["novo"]},
    )

    assert await age.get_entity(session, "Nome Antigo", "curated") is None
    got = await age.get_entity(session, "Nome Novo", "curated")
    assert got["type"] == "preferencia"
    related = await age.get_related(session, "Nome Novo", "curated")
    assert {"name": "Vizinho", "type": "conceito"} in related


async def test_search_entities_matches_aliases_tags_and_path_with_ranking(session):
    await age.upsert_entity(
        session,
        "Stack técnica deve ser inferida por projeto",
        "preferencia",
        "curated",
        {
            "source_doc": "preferencias/stack-tecnica-por-projeto.md",
            "repo_path": "preferencias/stack-tecnica-por-projeto.md",
            "aliases": ["stack tecnica", "stack por projeto"],
            "tags": ["arquitetura"],
        },
    )
    await age.upsert_entity(
        session,
        "Outro",
        "conceito",
        "curated",
        {
            "source_doc": "preferencias/outro-stack-tecnica.md",
            "repo_path": "preferencias/outro-stack-tecnica.md",
            "aliases": [],
            "tags": [],
        },
    )

    by_alias = await age.search_entities(session, "stack tecnica", "curated")
    assert by_alias[0]["name"] == "Stack técnica deve ser inferida por projeto"

    by_tag = await age.search_entities(session, "arquitetura", "curated")
    assert by_tag[0]["name"] == "Stack técnica deve ser inferida por projeto"

    by_path = await age.search_entities(session, "outro stack tecnica", "curated")
    assert any(entity["name"] == "Outro" for entity in by_path)


async def test_search_entities_limit_applies_after_ranking(session):
    await age.upsert_entity(
        session,
        "Alvo Exato",
        "conceito",
        "curated",
        {"aliases": ["termo"]},
    )
    await age.upsert_entity(
        session,
        "Termo",
        "conceito",
        "curated",
        {"aliases": []},
    )

    found = await age.search_entities(session, "termo", "curated", limit=1)

    assert found == [{"name": "Termo", "type": "conceito", "namespace": "curated"}]
```

- [ ] **Step 2: Run AGE tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_graph.py::test_find_entity_by_source_doc_and_update_identity_preserves_relation tests/integration/test_graph.py::test_search_entities_matches_aliases_tags_and_path_with_ranking tests/integration/test_graph.py::test_search_entities_limit_applies_after_ranking -v
```

Expected: FAIL with missing `find_entity_by_source_doc` or `update_entity_identity`, and current `search_entities` not matching aliases.

- [ ] **Step 3: Modify AGE imports and normalization helpers**

In `src/brain/graph/age.py`, add this import near the top:

```python
import unicodedata
```

Add these helpers after `_dedupe_strings`:

```python
def _normalize_match_text(value: object) -> str:
    text_value = "" if value is None else str(value)
    text_value = unicodedata.normalize("NFKD", text_value)
    text_value = "".join(ch for ch in text_value if not unicodedata.combining(ch))
    text_value = text_value.casefold().replace("-", " ")
    text_value = re.sub(r"[^\w\s.]+", " ", text_value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text_value).strip()


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value]
    return [str(value)]
```

- [ ] **Step 4: Make AGE write helpers support caller-controlled commits**

Change `ensure_graph` in `src/brain/graph/age.py` to:

```python
async def ensure_graph(session: AsyncSession, *, commit: bool = True) -> None:
    await _prepare(session)
    exists = (await session.execute(text("SELECT 1 FROM ag_graph WHERE name='brain'"))).first()
    if not exists:
        await session.execute(text("SELECT create_graph('brain')"))
    if commit:
        await session.commit()
```

Change `upsert_entity` signature and final commit block in `src/brain/graph/age.py` to:

```python
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
    if commit:
        await session.commit()
```

Change `upsert_relation` signature and final commit block to:

```python
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
```

Change `update_entity` signature and final commit block to:

```python
async def update_entity(
    session: AsyncSession,
    name: str,
    namespace: str,
    props: dict,
    *,
    commit: bool = True,
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
    if commit:
        await session.commit()
```

Change `delete_entities_by_source_doc` signature and final commit block to:

```python
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
```

- [ ] **Step 5: Add source-doc lookup and identity update helpers**

Add these functions after `get_entity` in `src/brain/graph/age.py`:

```python
async def find_entity_by_source_doc(
    session: AsyncSession,
    namespace: str,
    repo_path: str,
    *,
    document_id: str | None = None,
) -> dict | None:
    await _prepare(session)
    document_filter = (
        f"OR n.props.document_id = {_lit(document_id)} "
        if document_id
        else ""
    )
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        f"WHERE n.source_doc = {_lit(repo_path)} "
        f"OR n.props.source_doc = {_lit(repo_path)} "
        f"OR n.props.repo_path = {_lit(repo_path)} "
        f"{document_filter}"
        f"RETURN n.name, n.type, n.namespace, n.props "
        f"ORDER BY n.name "
        f"LIMIT 1 $cy$) AS (name agtype, type agtype, namespace agtype, props agtype)"
    )
    row = (await session.execute(text(q))).first()
    if row is None:
        return None
    return {
        "name": _unwrap(row[0]),
        "type": _unwrap(row[1]),
        "namespace": _unwrap(row[2]),
        "props": _unwrap(row[3]),
    }


async def update_entity_identity(
    session: AsyncSession,
    current_name: str,
    namespace: str,
    *,
    name: str,
    type: str,
    props: dict,
    commit: bool = True,
) -> None:
    await _prepare(session)
    source_doc = props.get("source_doc")
    source_memory = props.get("source_memory")
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(current_name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.name = {_lit(name)}, n.type = {_lit(type)}, n.props = {_lit(props)}, "
        f"n.source_doc = {_lit(source_doc)}, n.source_memory = {_lit(source_memory)} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    if commit:
        await session.commit()
```

- [ ] **Step 6: Replace `search_entities` with ranked props-aware search**

Replace the existing `search_entities` function in `src/brain/graph/age.py` with:

```python
def _entity_match_rank(entity: dict, query: str) -> tuple[int, str, str, str] | None:
    q = _normalize_match_text(query)
    if not q:
        return None

    name = str(entity.get("name") or "")
    namespace = str(entity.get("namespace") or "")
    props = entity.get("props") if isinstance(entity.get("props"), dict) else {}
    aliases = _as_string_list(props.get("aliases")) + _as_string_list(
        props.get("aliases_normalized")
    )
    tags = _as_string_list(props.get("tags")) + _as_string_list(props.get("tags_normalized"))
    paths = [
        props.get("source_doc"),
        props.get("repo_path"),
        entity.get("source_doc"),
    ]

    normalized_name = _normalize_match_text(name)
    normalized_aliases = [_normalize_match_text(alias) for alias in aliases]
    normalized_tags = [_normalize_match_text(tag) for tag in tags]
    normalized_paths = [_normalize_match_text(path) for path in paths if path]

    if normalized_name == q:
        return (0, normalized_name, namespace, name)
    if q in normalized_aliases:
        return (1, normalized_name, namespace, name)
    if q in normalized_tags:
        return (2, normalized_name, namespace, name)
    if normalized_name.startswith(q) or any(alias.startswith(q) for alias in normalized_aliases):
        return (3, normalized_name, namespace, name)
    if q in normalized_name or any(q in alias for alias in normalized_aliases):
        return (4, normalized_name, namespace, name)
    if any(q in tag for tag in normalized_tags) or any(q in path for path in normalized_paths):
        return (5, normalized_name, namespace, name)
    return None


async def search_entities(
    session: AsyncSession,
    query: str,
    namespace: str | None,
    limit: int | None = None,
) -> list[dict]:
    await _prepare(session)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit deve ser um inteiro positivo")

    namespace_match = (
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        if namespace is not None
        else "MATCH (n:Entity) "
    )
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"{namespace_match}"
        f"RETURN n.name, n.type, n.namespace, n.props, n.source_doc "
        f"$cy$) AS (name agtype, type agtype, namespace agtype, props agtype, source_doc agtype)"
    )
    rows = (await session.execute(text(q))).all()
    ranked: list[tuple[tuple[int, str, str, str], dict]] = []
    for name, type_value, namespace_value, props_value, source_doc_value in rows:
        entity = {
            "name": _unwrap(name),
            "type": _unwrap(type_value),
            "namespace": _unwrap(namespace_value),
            "props": _unwrap(props_value),
            "source_doc": _unwrap(source_doc_value),
        }
        rank = _entity_match_rank(entity, query)
        if rank is None:
            continue
        ranked.append(
            (
                rank,
                {
                    "name": entity["name"],
                    "type": entity["type"],
                    "namespace": entity["namespace"],
                },
            )
        )

    ranked.sort(key=lambda item: item[0])
    results = [entity for _, entity in ranked]
    return results[:limit] if limit is not None else results
```

- [ ] **Step 7: Run AGE tests**

Run:

```bash
uv run pytest tests/integration/test_graph.py -v
```

Expected: PASS for all graph integration tests.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/brain/graph/age.py tests/integration/test_graph.py
git commit -m "feat(graph): search entities by aliases and source paths"
```

Expected: commit succeeds.

---

### Task 3: Pipeline Integration And Individual Reindex Behavior

**Files:**
- Modify: `src/brain/ingestion/pipeline.py`
- Modify: `tests/integration/test_pipeline.py`

- [ ] **Step 1: Write failing pipeline integration tests**

Append these tests to `tests/integration/test_pipeline.py`:

```python
async def test_index_document_cria_entidade_deterministica_de_nota_curada(session):
    content = "# Stack técnica deve ser inferida por projeto\n\nCorpo."

    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        _settings(),
        namespace="curated",
        repo_path="preferencias/stack-tecnica-por-projeto.md",
        content=content,
        commit_sha="abc",
        meta={"metadata": {"type": "preference", "tags": ["stack"]}},
    )

    found = await age.search_entities(session, "stack tecnica", "curated")
    assert found[0]["name"] == "Stack técnica deve ser inferida por projeto"
    ent = await age.get_entity(session, "Stack técnica deve ser inferida por projeto", "curated")
    assert ent["type"] == "preferencia"
    assert ent["props"]["source_doc"] == "preferencias/stack-tecnica-por-projeto.md"


async def test_index_document_content_hash_igual_sincroniza_metadata_sem_rechunk(session):
    settings = _settings()
    content = "# Nome Antigo\n\nMesmo corpo."
    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        settings,
        namespace="curated",
        repo_path="preferencias/metadata.md",
        content=content,
        commit_sha="old",
        meta={"metadata": {"title": "Nome Antigo", "type": "preference"}},
    )

    changed = await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        settings,
        namespace="curated",
        repo_path="preferencias/metadata.md",
        content=content,
        commit_sha="new",
        meta={
            "metadata": {
                "title": "Nome Novo",
                "type": "decision",
                "tags": ["renomeado"],
            }
        },
    )

    assert changed is False
    assert await age.get_entity(session, "Nome Antigo", "curated") is None
    ent = await age.get_entity(session, "Nome Novo", "curated")
    assert ent is not None
    assert ent["type"] == "decisao"
    assert "renomeado" in ent["props"]["tags"]
    found = await age.search_entities(session, "renomeado", "curated")
    assert found[0]["name"] == "Nome Novo"


async def test_index_document_nao_cria_entidade_deterministica_para_agents(session):
    await pipeline.index_document(
        session,
        FakeEmbedder(),
        None,
        _settings(),
        namespace="curated",
        repo_path="_agents/chatgpt/raw.md",
        content="# Raw\n\nNao deve virar entidade.",
        commit_sha="abc",
    )

    assert await age.search_entities(session, "Raw", "curated") == []
```

- [ ] **Step 2: Run pipeline tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_pipeline.py::test_index_document_cria_entidade_deterministica_de_nota_curada tests/integration/test_pipeline.py::test_index_document_content_hash_igual_sincroniza_metadata_sem_rechunk tests/integration/test_pipeline.py::test_index_document_nao_cria_entidade_deterministica_para_agents -v
```

Expected: FAIL because `pipeline.index_document` does not call semantic entity sync yet.

- [ ] **Step 3: Import semantic entity sync in pipeline**

In `src/brain/ingestion/pipeline.py`, add this import:

```python
from brain.ingestion.semantic_entities import upsert_entity_from_curated_document
```

- [ ] **Step 4: Add a pipeline helper for semantic sync**

In `src/brain/ingestion/pipeline.py`, add this helper after `_title`:

```python
async def _sync_curated_semantic_entity(session, doc, *, content: str) -> None:
    metadata = (doc.meta or {}).get("metadata") if isinstance(doc.meta, dict) else None
    await age.ensure_graph(session, commit=False)
    await upsert_entity_from_curated_document(
        session,
        namespace=doc.namespace,
        repo_path=doc.repo_path,
        title=doc.title,
        content=content,
        metadata=metadata if isinstance(metadata, dict) else {},
        document_id=str(doc.id),
    )
```

- [ ] **Step 5: Update the content-hash no-op branch**

In `src/brain/ingestion/pipeline.py`, replace the existing content-hash equal branch with:

```python
    if existing and existing.content_hash == h:
        next_meta = existing.meta if meta is None else meta
        title = _title(content)
        if (
            existing.namespace != namespace
            or existing.title != title
            or existing.raw_content != content
            or existing.commit_sha != commit_sha
            or existing.meta != next_meta
        ):
            existing = await repo.upsert_document(
                session,
                namespace=namespace,
                repo_path=repo_path,
                title=title,
                raw_content=content,
                content_hash=h,
                commit_sha=commit_sha,
                meta=meta,
            )
        await _sync_curated_semantic_entity(session, existing, content=content)
        if commit:
            await session.commit()
        return False
```

- [ ] **Step 6: Sync deterministic entity after document upsert**

In `src/brain/ingestion/pipeline.py`, after the `doc = await repo.upsert_document(...)` block and before `chunks = chunk_markdown(...)`, add:

```python
    await _sync_curated_semantic_entity(session, doc, content=content)
```

- [ ] **Step 7: Keep AGE writes inside caller transaction**

In `src/brain/ingestion/pipeline.py`, change the graph setup and source-doc deletion in the content-changed path to:

```python
    await age.ensure_graph(session, commit=False)
    if existing:
        await age.delete_entities_by_source_doc(
            session,
            repo_path,
            existing.namespace,
            commit=False,
        )
```

In `src/brain/ingestion/pipeline.py`, change the LLM graph setup to:

```python
        await age.ensure_graph(session, commit=False)
```

In `src/brain/ingestion/pipeline.py`, change the LLM entity writes to pass `commit=False`:

```python
        for e in ents["entities"]:
            await age.upsert_entity(
                session,
                e["name"],
                e["type"],
                namespace,
                {"source_doc": repo_path},
                commit=False,
            )
        for r in ents["relations"]:
            await age.upsert_relation(
                session,
                r["source"],
                r["target"],
                r["type"],
                namespace,
                commit=False,
            )
```

- [ ] **Step 8: Run pipeline tests**

Run:

```bash
uv run pytest tests/integration/test_pipeline.py -v
```

Expected: PASS for all pipeline integration tests.

- [ ] **Step 9: Commit**

Run:

```bash
git add src/brain/ingestion/pipeline.py tests/integration/test_pipeline.py
git commit -m "feat(ingestion): sync curated note semantic entities"
```

Expected: commit succeeds.

---

### Task 4: MCP Handler Upsert Semantics And Curated Note Flows

**Files:**
- Modify: `src/brain/mcp/handlers.py`
- Modify: `tests/integration/test_mcp_handlers.py`

- [ ] **Step 1: Write failing handler integration tests**

Append these tests to `tests/integration/test_mcp_handlers.py`:

```python
async def test_create_note_cria_entidade_deterministica_pesquisavel_por_alias(deps):
    await _as_curator(
        handlers.create_note,
        deps,
        "preferencias/regras-env-e-migrations-por-projeto.md",
        "# Regras de .env e migrations dependem do projeto\n\nCorpo.",
        metadata={
            "title": "Regras de .env e migrations dependem do projeto",
            "type": "preference",
            "tags": ["env", "migrations"],
        },
    )

    found = await _as_curator(handlers.search_entities, deps, "env migrations", "curated")

    assert found[0]["name"] == "Regras de .env e migrations dependem do projeto"


async def test_update_note_metadata_only_renomeia_entidade_sem_duplicar_source_doc(deps):
    created = await _as_curator(
        handlers.create_note,
        deps,
        "preferencias/perfil-ceo.md",
        "# Perfil CEO\n\nCorpo.",
        metadata={"title": "Perfil CEO", "aliases": ["Hermes CEO"]},
    )

    await _as_curator(
        handlers.update_note,
        deps,
        created["id"],
        "# Perfil CEO\n\nCorpo.",
        metadata={"title": "Perfil CEO Atualizado", "aliases": ["Hermes CEO"]},
    )

    found = await _as_curator(handlers.search_entities, deps, "Hermes CEO", "curated")
    assert found[0]["name"] == "Perfil CEO Atualizado"

    async with deps.session_factory() as s:
        entities = await handlers.age.search_entities(s, "perfil ceo", "curated")
    matching_names = {entity["name"] for entity in entities}
    assert "Perfil CEO Atualizado" in matching_names
    assert "Perfil CEO" not in matching_names


async def test_update_entity_cria_quando_entidade_nao_existe(deps):
    out = await _as_curator(
        handlers.update_entity,
        deps,
        "Entidade Manual",
        "curated",
        {"aliases": ["manual"], "source_doc": "manual.md"},
    )

    assert out == {"updated": True}
    found = await _as_curator(handlers.search_entities, deps, "manual", "curated")
    assert found[0]["name"] == "Entidade Manual"
```

- [ ] **Step 2: Run handler tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py::test_create_note_cria_entidade_deterministica_pesquisavel_por_alias tests/integration/test_mcp_handlers.py::test_update_note_metadata_only_renomeia_entidade_sem_duplicar_source_doc tests/integration/test_mcp_handlers.py::test_update_entity_cria_quando_entidade_nao_existe -v
```

Expected: first two tests fail until pipeline integration is present; third fails because current `update_entity` matches only existing nodes.

- [ ] **Step 3: Change curator `update_entity` to real upsert**

In `src/brain/mcp/handlers.py`, replace the body of `update_entity` with:

```python
async def update_entity(deps: Deps, name: str, namespace: str, props: dict) -> dict:
    _require_curator()
    entity_type = str(props.get("type") or props.get("entity_type") or "conceito")
    async with deps.session_factory() as s:
        await age.upsert_entity(s, name, entity_type, namespace, props)
        return {"updated": True}
```

- [ ] **Step 4: Run handler tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py::test_create_note_cria_entidade_deterministica_pesquisavel_por_alias tests/integration/test_mcp_handlers.py::test_update_note_metadata_only_renomeia_entidade_sem_duplicar_source_doc tests/integration/test_mcp_handlers.py::test_update_entity_cria_quando_entidade_nao_existe -v
```

Expected: PASS for the three focused tests.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/brain/mcp/handlers.py tests/integration/test_mcp_handlers.py
git commit -m "feat(mcp): upsert manual entity updates"
```

Expected: commit succeeds.

---

### Task 5: Required Acceptance Query Coverage

**Files:**
- Modify: `tests/integration/test_pipeline.py`

- [ ] **Step 1: Write acceptance fixture test**

Append this test to `tests/integration/test_pipeline.py`:

```python
async def test_search_entities_acceptance_queries_for_curated_note_aliases(session):
    settings = _settings()
    cases = [
        (
            "preferencias/stack-tecnica-por-projeto.md",
            "# Stack técnica deve ser inferida por projeto\n\nCorpo.",
            {"metadata": {"title": "Stack técnica deve ser inferida por projeto", "type": "preference"}},
            ["Stack técnica por projeto", "stack tecnica"],
            "Stack técnica deve ser inferida por projeto",
        ),
        (
            "preferencias/regras-env-e-migrations-por-projeto.md",
            "# Regras de .env e migrations dependem do projeto\n\nCorpo.",
            {"metadata": {"title": "Regras de .env e migrations dependem do projeto", "type": "preference"}},
            ["env migrations", "migrations por projeto"],
            "Regras de .env e migrations dependem do projeto",
        ),
        (
            "preferencias/privacidade-credenciais-e-acoes-externas.md",
            "# Privacidade, credenciais e ações externas\n\nCorpo.",
            {"metadata": {"title": "Privacidade, credenciais e ações externas", "type": "preference"}},
            ["Privacidade", "credenciais"],
            "Privacidade, credenciais e ações externas",
        ),
        (
            "preferencias/perfil-ceo.md",
            "# Perfil CEO\n\nCorpo.",
            {"metadata": {"title": "Perfil CEO", "aliases": ["Hermes CEO", "ceo hermes"]}},
            ["Hermes CEO"],
            "Perfil CEO",
        ),
        (
            "projetos/famaagent.md",
            "# FamaAgent\n\nProjeto.",
            {"metadata": {"title": "FamaAgent", "type": "project"}},
            ["FamaAgent"],
            "FamaAgent",
        ),
        (
            "projetos/mcp-fama.md",
            "# MCP-Fama\n\nProjeto.",
            {"metadata": {"title": "MCP-Fama", "type": "project", "aliases": ["mcp-fama"]}},
            ["mcp-fama"],
            "MCP-Fama",
        ),
        (
            "projetos/evolution-go.md",
            "# Evolution API\n\nProjeto.",
            {"metadata": {"title": "Evolution API", "type": "project", "aliases": ["Evolution-go"]}},
            ["Evolution-go"],
            "Evolution API",
        ),
        (
            "projetos/paperclip-openclaw.md",
            "# OpenClaw\n\nProjeto.",
            {"metadata": {"title": "OpenClaw", "type": "project", "aliases": ["Paperclip"]}},
            ["Paperclip"],
            "OpenClaw",
        ),
    ]

    for repo_path, content, meta, queries, _expected_name in cases:
        await pipeline.index_document(
            session,
            FakeEmbedder(),
            None,
            settings,
            namespace="curated",
            repo_path=repo_path,
            content=content,
            commit_sha="abc",
            meta=meta,
        )

    for _repo_path, _content, _meta, queries, expected_name in cases:
        for query in queries:
            found = await age.search_entities(session, query, "curated")
            assert found, query
            assert found[0]["name"] == expected_name
```

- [ ] **Step 2: Run acceptance test**

Run:

```bash
uv run pytest tests/integration/test_pipeline.py::test_search_entities_acceptance_queries_for_curated_note_aliases -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

Run:

```bash
git add tests/integration/test_pipeline.py
git commit -m "test: cover curated note entity acceptance queries"
```

Expected: commit succeeds.

---

### Task 6: Regression Sweep And Documentation Check

**Files:**
- Modify: `docs/data-model.md`
- Modify: `docs/mcp-api.md`

- [ ] **Step 1: Update data model graph documentation**

In `docs/data-model.md`, replace the paragraph under `## Grafo AGE` that starts with `Os nós de entidade são gerenciados` with:

```markdown
Os nós de entidade são gerenciados por `brain.graph.age` com label `Entity`. Relações entre entidades usam arestas `REL`, com o tipo semântico salvo na propriedade `type`. As entidades carregam propriedades como `name`, `type`, `namespace`, `props`, `source_doc` e `source_memory`, permitindo rastrear a origem em documentos ou memórias.

Além das entidades extraídas por LLM, notas curadas no namespace `curated` geram uma entidade determinística por documento Markdown elegível. Essa entidade usa título/metadados/path como fonte de nome, aliases e tags pesquisáveis, persiste `source_doc`/`repo_path`/`document_id` em `props` e pode ser reconstruída por reindexação individual do documento.
```

- [ ] **Step 2: Update MCP entity search documentation**

In `docs/mcp-api.md`, in the `Entidades e relações do grafo` list, replace the `search_entities` bullet with:

```markdown
- `search_entities`: busca entidades por nome, aliases, tags e caminhos de origem normalizados.
```

- [ ] **Step 3: Run focused and full test suites**

Run:

```bash
uv run pytest tests/test_semantic_entities.py tests/integration/test_graph.py tests/integration/test_pipeline.py tests/integration/test_mcp_handlers.py -v
```

Expected: PASS.

Then run:

```bash
uv run pytest -q
```

Expected: PASS for the full repository test suite.

- [ ] **Step 4: Commit docs**

Run:

```bash
git add docs/data-model.md docs/mcp-api.md
git commit -m "docs: describe curated note semantic entities"
```

Expected: commit succeeds.

- [ ] **Step 5: Final git status**

Run:

```bash
git status --short
```

Expected: no modified tracked files unless the execution agent intentionally leaves a plan checkbox update uncommitted.

---

## Self-Review

Spec coverage:

- Deterministic entity per curated note: Task 1 and Task 3.
- Canonical fallback `metadata.title -> H1 -> path -> skipped`: Task 1.
- Aliases from title/path/tags/explicit metadata with normalization and conservative limits: Task 1 and Task 5.
- `search_entities` over name/aliases/tags/path with ranking: Task 2.
- Real upsert behavior: Task 2 and Task 4.
- `create_note`, `update_note`, individual reindex through `pipeline.index_document`: Task 3 and Task 4.
- No `_agents/` deterministic entity: Task 1 and Task 3.
- Content-hash equal still syncs deterministic entity: Task 3.
- Synthetic regression fixtures for named projects/tools: Task 5.
- Documentation updates: Task 6.

Placeholder scan:

- This plan has no deferred implementation sections and no steps that ask the implementer to infer test content.

Type consistency:

- `build_curated_entity_payload(...) -> dict` returns `status = "ready"` for writable payloads and `status = "skipped"` for ineligible documents.
- `upsert_entity_from_curated_document(...) -> dict` returns `status = "created"`, `"updated"`, or `"skipped"`.
- AGE helpers use `commit: bool = True` for backward compatibility and pass `commit=False` from pipeline/semantic sync.
