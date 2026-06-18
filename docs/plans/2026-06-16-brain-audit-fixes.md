# Brain Audit Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Corrigir os problemas encontrados na auditoria do provedor `brain`.

**Architecture:** Manter o projeto achatado na raiz, com pacote Python importado como `brain`. Compartilhar `repo_cache` entre API e worker no Docker, garantir inicializacao do clone antes de `remember`, preservar metadados, evitar credenciais persistidas no remote Git e limpar entidades de grafo por documento reindexado/removido.

**Tech Stack:** Python 3.12, uv, pytest/pytest-asyncio, SQLAlchemy async, pgvector, Apache AGE, FastAPI, MCP.

---

### Task 1: Tests for Regressions

**Files:**
- Modify: `tests/test_git_sync.py`
- Modify: `tests/test_git_writer.py`
- Modify: `tests/integration/test_mcp_handlers.py`
- Modify: `tests/integration/test_worker.py`
- Modify: `tests/integration/test_pipeline.py`
- Modify: `tests/integration/test_graph.py`
- Modify: `tests/integration/test_main.py`
- Create: `tests/test_imports.py`
- Create: `tests/test_deploy_config.py`

**Steps:**
1. Add focused failing tests for import namespace, Docker shared cache volume, clone auth, markdown rename, remember clone+metadata, worker metadata, graph props, graph cleanup, and brain-bot webhook ignore.
2. Run the focused tests and confirm failures match the audited issues.

### Task 2: Packaging and Docker

**Files:**
- Modify: `src/brain/**/*.py`
- Modify: `docker-compose.yml`

**Steps:**
1. Replace `src.brain` internal imports with `brain`.
2. Add a shared `repo_cache` named volume mounted at `/app/repo_cache` for both `api` and `worker`.
3. Keep Postgres volume unchanged.

### Task 3: Git and Remember Flow

**Files:**
- Modify: `src/brain/ingestion/git_sync.py`
- Modify: `src/brain/ingestion/git_writer.py`
- Modify: `src/brain/mcp/handlers.py`
- Modify: `src/brain/worker.py`
- Modify: `src/brain/ingestion/pipeline.py`

**Steps:**
1. Pass GitHub auth through per-command extra headers rather than embedding the token in clone URLs.
2. Expand markdown renames to delete old path and add new path.
3. Ensure `remember` clones/pulls the repo cache before writing a conversation.
4. Persist metadata into the markdown note and the `extract_facts` job payload.
5. Pass metadata through worker and pipeline into `Memory.meta`.

### Task 4: Graph Consistency

**Files:**
- Modify: `src/brain/graph/age.py`
- Modify: `src/brain/ingestion/pipeline.py`
- Modify: `src/brain/worker.py`

**Steps:**
1. Store graph `props` as AGE maps, not JSON strings.
2. Expose deletion of entities by `source_doc`.
3. Remove previous source-doc entities before reindexing the same document.
4. Remove source-doc entities when a document delete job runs.

### Task 5: Verification and Git State

**Files:**
- Modify: `tests/integration/test_migrations.py`
- Local env: `.venv`

**Steps:**
1. Use `python -m alembic` in the migration integration test to avoid stale script wrappers.
2. Reinstall/sync the local virtualenv so `uv run pytest` resolves wrappers correctly.
3. Run focused tests and full suite.
4. Stage the root-layout move intentionally, excluding unrelated napkin notes.
