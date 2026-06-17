# Agent Inbox Curation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the current `remember` flow with a client inbox + Hermes curation flow where clients submit raw agent notes, Hermes creates curated notes, and `search` reads only curated knowledge.

**Architecture:** Keep the existing modular Python/FastAPI/MCP service. Add principal-aware auth, client token management, raw agent-note inbox tables, curated-note writing helpers, an outbox webhook dispatcher, and Obsidian-style link extraction. Reuse the existing Git writer, document/chunk indexing, Postgres queue patterns, and MCP handler style where possible.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, SQLAlchemy async, Alembic, pgvector, Apache AGE, OpenAI embeddings/LLM wrappers, `cryptography` for recoverable client tokens, `httpx` for Hermes webhook delivery, pytest/testcontainers.

---

## Baseline

Worktree:

```text
/root/.config/superpowers/worktrees/brain/agent-inbox-curation
```

All commands below run from:

```bash
cd /root/.config/superpowers/worktrees/brain/agent-inbox-curation/brain
```

Fresh baseline before writing this plan:

```bash
uv sync
uv run pytest
```

Expected current result:

```text
63 passed, 1 warning
```

Reference design:

```text
../docs/plans/2026-06-17-agent-inbox-curation-design.md
```

## Implementation Notes

- Preserve the old code until the new path is covered by tests. Remove `remember` near the end.
- Keep `_agents/` out of curated indexing and search.
- Clients must never pass `client` or `path` to `submit_agent_note`.
- Hermes is not an `AgentClient`; it is a curator principal resolved from env.
- Store client token hash + encrypted token in Postgres. Never write full tokens to Git.
- Prefer whole-note replacement for `update_note`.
- Start with exact link extraction and simple resolution. Do not overbuild fuzzy matching.

---

### Task 1: Config, Token Crypto, and Principal Auth

**Files:**
- Modify: `brain/pyproject.toml`
- Modify: `brain/src/brain/config.py`
- Modify: `brain/src/brain/auth.py`
- Test: `brain/tests/test_config.py`
- Test: `brain/tests/test_auth.py`

**Step 1: Add failing config tests**

Add tests for the new curator and encryption settings:

```python
from cryptography.fernet import Fernet


def test_settings_curator_bootstrap_fields():
    key = Fernet.generate_key().decode()
    s = Settings(
        database_url="postgresql+asyncpg://x",
        openai_api_key="sk-test",
        github_token="ghp_test",
        brain_auth_token="legacy",
        webhook_secret="webhook",
        repo_url="https://example/repo.git",
        brain_curator_slug="hermes",
        brain_curator_name="Hermes",
        brain_curator_token="curator-token",
        brain_token_encryption_key=key,
    )
    assert s.brain_curator_slug == "hermes"
    assert s.brain_curator_name == "Hermes"
    assert s.brain_curator_token == "curator-token"
    assert s.brain_token_encryption_key == key
```

Add auth tests:

```python
from cryptography.fernet import Fernet

from brain import auth


def test_token_crypto_roundtrip():
    key = Fernet.generate_key().decode()
    token = auth.generate_client_token("chatgpt-web")
    encrypted = auth.encrypt_token(token, key)
    assert encrypted != token
    assert auth.decrypt_token(encrypted, key) == token
    assert auth.hash_token(token) == auth.hash_token(token)


def test_principal_context_roundtrip():
    principal = auth.Principal(type="client", slug="chatgpt-web", name="ChatGPT Web")
    token = auth.set_current_principal(principal)
    try:
        assert auth.get_current_principal() == principal
    finally:
        auth.reset_current_principal(token)
```

**Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/test_config.py tests/test_auth.py -v
```

Expected: FAIL because new settings/helpers do not exist.

**Step 3: Add dependency**

Run:

```bash
uv add cryptography
```

Expected: `pyproject.toml` and `uv.lock` update.

**Step 4: Implement settings**

In `brain/src/brain/config.py`, add:

```python
    # Curadoria / auth
    brain_curator_slug: str = "hermes"
    brain_curator_name: str = "Hermes"
    brain_curator_token: str
    brain_token_encryption_key: str

    # Agent inbox
    agent_inbox_dir: str = "_agents"

    # Hermes webhook
    hermes_webhook_url: str | None = None
    hermes_webhook_secret: str | None = None
    outbox_max_attempts: int = 8
```

Keep `brain_auth_token` for temporary compatibility during the migration. It can be removed in a later cleanup only after MCP auth is fully moved.

**Step 5: Implement auth helpers**

In `brain/src/brain/auth.py`, keep `verify_bearer_token` for existing tests and add:

```python
import contextvars
import hashlib
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Principal:
    type: str  # "curator" | "client"
    slug: str
    name: str


_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "brain_current_principal", default=None
)


def generate_client_token(slug: str) -> str:
    return f"brain_client_{slug}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def encrypt_token(token: str, key: str) -> str:
    return Fernet(key.encode("utf-8")).encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted: str, key: str) -> str:
    return Fernet(key.encode("utf-8")).decrypt(encrypted.encode("utf-8")).decode("utf-8")


def set_current_principal(principal: Principal):
    return _current_principal.set(principal)


def reset_current_principal(token) -> None:
    _current_principal.reset(token)


def get_current_principal() -> Principal:
    principal = _current_principal.get()
    if principal is None:
        raise AuthError("Principal ausente")
    return principal
```

**Step 6: Run tests**

Run:

```bash
uv run pytest tests/test_config.py tests/test_auth.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/brain/config.py src/brain/auth.py tests/test_config.py tests/test_auth.py
git commit -m "feat(brain): adiciona principals e criptografia de tokens"
```

---

### Task 2: Database Models and Migration

**Files:**
- Modify: `brain/src/brain/storage/models.py`
- Create: `brain/migrations/versions/0002_agent_inbox_curated_notes.py`
- Test: `brain/tests/integration/test_migrations.py`
- Test: `brain/tests/integration/test_models.py`

**Step 1: Write failing model/migration tests**

Add assertions that these tables exist after migration:

```python
expected = {
    "agent_clients",
    "agent_notes",
    "outbox_events",
    "note_links",
}
assert expected <= set(tables)
```

Add a model smoke test:

```python
from brain.storage.models import AgentClient, AgentNote, NoteLink, OutboxEvent


async def test_agent_inbox_models_insert(session):
    client = AgentClient(
        slug="chatgpt-web",
        name="ChatGPT Web",
        token_prefix="brain_client_chatgpt-web",
        token_hash="hash",
        token_encrypted="encrypted",
        permissions=["search", "get_note", "submit_agent_note"],
        meta={"host": "chatgpt"},
    )
    session.add(client)
    await session.flush()

    note = AgentNote(
        client_id=client.id,
        client_slug=client.slug,
        title="Resumo",
        repo_path="_agents/chatgpt-web/2026/06/17/resumo.md",
        status="pending",
        metadata={"model": "gpt"},
    )
    session.add(note)
    await session.flush()

    event = OutboxEvent(type="agent_note.created", payload={"note_id": str(note.id)})
    link = NoteLink(source_document_id=None, source_path="brain.md", target="MCP", raw="[[MCP]]")
    session.add_all([event, link])
    await session.commit()
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_migrations.py tests/integration/test_models.py -v
```

Expected: FAIL because models/tables do not exist.

**Step 3: Add models**

Add to `brain/src/brain/storage/models.py`:

```python
from sqlalchemy import Boolean
from sqlalchemy.dialects.postgresql import ARRAY
```

Add `Document.meta`:

```python
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
```

Add models:

```python
class AgentClient(Base):
    __tablename__ = "agent_clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    token_prefix: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    token_encrypted: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Add `AgentNote`, `OutboxEvent`, and `NoteLink` with UUID primary keys, JSONB payload/metadata, status fields, timestamps, and indexes on status/type/client_slug/source_path/target.

Use `meta = mapped_column("metadata", JSONB, default=dict)` for ORM attributes named `meta`, because `metadata` is reserved on declarative models.

**Step 4: Add migration**

Create `brain/migrations/versions/0002_agent_inbox_curated_notes.py`.

Migration must:

- Add nullable `metadata JSONB NOT NULL DEFAULT '{}'` to `documents`.
- Create `agent_clients`.
- Create `agent_notes`.
- Create `outbox_events`.
- Create `note_links`.
- Add indexes matching query paths.

**Step 5: Run migration/model tests**

Run:

```bash
uv run pytest tests/integration/test_migrations.py tests/integration/test_models.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/storage/models.py migrations/versions/0002_agent_inbox_curated_notes.py tests/integration/test_migrations.py tests/integration/test_models.py
git commit -m "feat(brain): adiciona schema da inbox de agentes"
```

---

### Task 3: Repositories for Clients, Agent Notes, Outbox, and Links

**Files:**
- Modify: `brain/src/brain/storage/repositories.py`
- Test: `brain/tests/integration/test_repositories.py`

**Step 1: Write failing repository tests**

Add tests for:

- create/get/list/disable `AgentClient`
- lookup client by token hash
- create/list/get/update status for `AgentNote`
- create/claim/mark `OutboxEvent`
- replace/list/resolve `NoteLink`
- document metadata persists

Example:

```python
async def test_agent_client_crud(session):
    c = await repo.create_agent_client(
        session,
        slug="chatgpt-web",
        name="ChatGPT Web",
        description="web client",
        token_prefix="brain_client_chatgpt-web",
        token_hash="hash",
        token_encrypted="encrypted",
        permissions=["search", "get_note", "submit_agent_note"],
        meta={"host": "chatgpt"},
    )
    await session.commit()
    assert (await repo.get_agent_client(session, slug="chatgpt-web")).id == c.id
    assert (await repo.get_agent_client_by_token_hash(session, "hash")).slug == "chatgpt-web"
    await repo.disable_agent_client(session, "chatgpt-web")
    await session.commit()
    assert (await repo.get_agent_client(session, slug="chatgpt-web")).status == "disabled"
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_repositories.py -v
```

Expected: FAIL.

**Step 3: Implement repository functions**

Add functions:

```python
async def create_agent_client(...)
async def get_agent_client(session, *, slug: str)
async def get_agent_client_by_token_hash(session, token_hash: str)
async def list_agent_clients(session)
async def update_agent_client_token(...)
async def disable_agent_client(session, slug: str)
async def touch_agent_client_seen(session, slug: str)

async def create_agent_note(...)
async def get_agent_note(session, id: uuid.UUID)
async def list_agent_notes(session, status=None, client_slug=None, limit=50)
async def update_agent_note_status(session, id, status, outcome=None, error=None)

async def create_outbox_event(session, type: str, payload: dict)
async def claim_next_outbox_event(session, now, worker_id: str)
async def mark_outbox_delivered(session, id)
async def mark_outbox_retrying(session, id, error, run_after)
async def mark_outbox_failed(session, id, error)

async def replace_note_links(session, source_document_id, source_path, links)
async def list_unresolved_links(session, limit=50)
async def resolve_note_link(session, link_id, target_path)
```

**Step 4: Run tests**

Run:

```bash
uv run pytest tests/integration/test_repositories.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/brain/storage/repositories.py tests/integration/test_repositories.py
git commit -m "feat(brain): adiciona repositorios da inbox"
```

---

### Task 4: Git Writers and Markdown Rendering

**Files:**
- Modify: `brain/src/brain/ingestion/git_writer.py`
- Test: `brain/tests/test_git_writer.py`

**Step 1: Write failing writer tests**

Add tests for:

- safe client slug/path generation
- agent client profile render does not include full token
- agent note writes under `_agents/{client_slug}/...`
- curated note cannot write under `_agents/`
- parent directories are created automatically
- messages render as simple Markdown

Example:

```python
def test_write_agent_note_cria_apenas_na_pasta_do_client(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = write_agent_note(
        repo,
        inbox_dir="_agents",
        client_slug="chatgpt-web",
        client_name="ChatGPT Web",
        note_id="agent_note_1",
        title="Resumo",
        content="Conteudo livre",
        messages=[{"role": "user", "content": "oi"}],
        suggested_namespace="brain",
        metadata={"model": "gpt"},
        timestamp="20260617T183000000000",
        author_name="brain-bot",
        author_email="brain-bot@example.com",
        push=False,
    )
    assert rel.startswith("_agents/chatgpt-web/2026/06/17/")
    text = (repo / rel).read_text()
    assert "Conteudo livre" in text
    assert "**user:** oi" in text
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_git_writer.py -v
```

Expected: FAIL.

**Step 3: Implement writer helpers**

Keep old `write_conversation` temporarily. Add:

```python
def slugify(text: str, *, fallback: str = "note") -> str
def validate_curated_note_path(path: str) -> str
def render_frontmatter(data: dict) -> str
def render_messages_markdown(messages: list[dict]) -> str
def render_agent_client_profile(...)
def render_agent_note(...)
def write_agent_client_profile(...)
def write_agent_note(...)
def write_curated_note(...)
```

Rules:

- `write_agent_note` computes path; callers do not pass a path.
- `write_curated_note` accepts validated path and creates parents.
- no writer writes a full token to Markdown.
- commit messages should identify the operation:
  - `client: create chatgpt-web`
  - `agent-note: chatgpt-web 20260617T...`
  - `note: create projetos/brain.md`
  - `note: update projetos/brain.md`

**Step 4: Run tests**

Run:

```bash
uv run pytest tests/test_git_writer.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/brain/ingestion/git_writer.py tests/test_git_writer.py
git commit -m "feat(brain): adiciona escritores de notas"
```

---

### Task 5: Principal-Aware MCP Auth Middleware

**Files:**
- Modify: `brain/src/brain/main.py`
- Modify: `brain/src/brain/auth.py`
- Test: `brain/tests/integration/test_main.py`

**Step 1: Write failing auth middleware tests**

Add tests:

- curator token is accepted
- client token is accepted and sets principal
- disabled client token is rejected
- unknown token is rejected

Use a temporary DB client with known `token_hash`.

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_main.py -v
```

Expected: FAIL.

**Step 3: Implement resolver**

In `auth.py`, add:

```python
async def resolve_principal(session, settings, bearer_token: str) -> Principal:
    if hmac.compare_digest(bearer_token, settings.brain_curator_token):
        return Principal("curator", settings.brain_curator_slug, settings.brain_curator_name)
    client = await repo.get_agent_client_by_token_hash(session, hash_token(bearer_token))
    if client is None or client.status != "active":
        raise AuthError("Token invalido")
    return Principal("client", client.slug, client.name)
```

Avoid a direct import cycle by importing repositories inside the function or placing resolver in a small module if needed.

**Step 4: Replace MCP middleware**

In `main.py`, replace `_BearerAuth` with `_PrincipalAuth`:

- parse `Authorization: Bearer <token>`
- open DB session with `sf`
- resolve principal
- set context var before calling MCP app
- reset context var after completion

Do not require auth for `/health`; `/status` can stay as is for now unless tests cover it.

**Step 5: Run tests**

Run:

```bash
uv run pytest tests/integration/test_main.py tests/test_auth.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/main.py src/brain/auth.py tests/integration/test_main.py tests/test_auth.py
git commit -m "feat(brain): autentica MCP por principal"
```

---

### Task 6: Agent Client Management Tools

**Files:**
- Modify: `brain/src/brain/mcp/handlers.py`
- Modify: `brain/src/brain/mcp/server.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`

**Step 1: Write failing handler tests**

Tests:

- curator can create agent client and receives token once
- profile Markdown exists and does not contain full token
- reveal returns encrypted token plaintext only for curator
- rotate changes token and invalidates old hash
- disable changes status
- client principal cannot create clients

Use auth context helpers:

```python
token = auth.set_current_principal(auth.Principal("curator", "hermes", "Hermes"))
try:
    out = await handlers.create_agent_client(...)
finally:
    auth.reset_current_principal(token)
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -v
```

Expected: FAIL.

**Step 3: Add permission helpers**

In `handlers.py`:

```python
def _require_curator():
    p = auth.get_current_principal()
    if p.type != "curator":
        raise PermissionError("curator required")
    return p


def _require_client_or_curator():
    return auth.get_current_principal()
```

**Step 4: Implement client handlers**

Add:

```python
async def create_agent_client(deps, name, slug=None, description=None, capture_policy=None, recommended_instructions=None, metadata=None)
async def list_agent_clients(deps)
async def get_agent_client(deps, slug)
async def reveal_agent_client_token(deps, slug)
async def rotate_agent_client_token(deps, slug)
async def disable_agent_client(deps, slug)
```

Use `git_writer.write_agent_client_profile` after DB insert/token creation. Commit DB and Git consistently enough for this single-user system; if Git write fails, let the handler error before commit where possible.

**Step 5: Register MCP tools**

In `server.py`, add tool wrappers for the six client management tools.

**Step 6: Run tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py tests/test_git_writer.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/brain/mcp/handlers.py src/brain/mcp/server.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): adiciona gestao de clients MCP"
```

---

### Task 7: submit_agent_note and Outbox Event Creation

**Files:**
- Modify: `brain/src/brain/mcp/handlers.py`
- Modify: `brain/src/brain/mcp/server.py`
- Modify: `brain/src/brain/config.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`

**Step 1: Write failing tests**

Tests:

- client can submit note without passing client/path
- note is written under `_agents/{client_slug}/...`
- DB `agent_notes.status` is `pending`
- an `outbox_events` row is created with type `agent_note.created`
- curator can submit only if explicitly treated as not client? Expected: reject curator for `submit_agent_note` to keep semantics clean
- content/messages validation rejects empty request

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -v
```

Expected: FAIL.

**Step 3: Implement handler**

Add:

```python
async def submit_agent_note(
    deps,
    title: str | None = None,
    content: str | None = None,
    messages: list[dict] | None = None,
    suggested_namespace: str | None = None,
    metadata: dict | None = None,
) -> dict:
    principal = _require_client()
    if not content and not messages:
        raise ValueError("content or messages required")
    ...
```

Flow:

1. Get active `AgentClient` by principal slug.
2. Create `AgentNote` row enough to get `id`.
3. Write Markdown with `write_agent_note`.
4. Update note `repo_path`.
5. Create outbox event payload with reference only.
6. Commit.
7. Return `note_id`, `repo_path`, `status`, `event_id`.

**Step 4: Register MCP tool**

In `server.py`:

```python
@mcp.tool()
async def submit_agent_note(title: str | None = None, content: str | None = None, messages: list[dict] | None = None, suggested_namespace: str | None = None, metadata: dict | None = None) -> dict:
    return await handlers.submit_agent_note(deps, title, content, messages, suggested_namespace, metadata)
```

**Step 5: Run tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py tests/test_git_writer.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/config.py src/brain/mcp/handlers.py src/brain/mcp/server.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): adiciona submissao de notas brutas"
```

---

### Task 8: Hermes Outbox Webhook Dispatcher

**Files:**
- Create: `brain/src/brain/outbox.py`
- Modify: `brain/src/brain/worker.py`
- Test: `brain/tests/integration/test_worker.py`
- Test: `brain/tests/test_signature.py`

**Step 1: Write failing tests**

Tests:

- HMAC header is computed from `timestamp + "." + raw_body`
- successful HTTP 2xx marks event `delivered`
- non-2xx marks `retrying` with attempts incremented
- too many attempts marks `failed`
- no `HERMES_WEBHOOK_URL` leaves event pending or marks failed with clear error; choose pending to avoid data loss

Use `httpx.MockTransport`.

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_worker.py tests/test_signature.py -v
```

Expected: FAIL.

**Step 3: Implement outbox module**

`brain/src/brain/outbox.py`:

```python
def sign_webhook(secret: str, timestamp: str, body: bytes) -> str

async def deliver_once(session_factory, settings, *, worker_id="outbox", client=None) -> bool:
    ...
```

Delivery:

- claim one pending/retrying event due now
- serialize JSON compactly and deterministically enough
- send POST to `settings.hermes_webhook_url`
- headers:
  - `X-Brain-Event-Id`
  - `X-Brain-Event-Type`
  - `X-Brain-Signature`
  - `X-Brain-Timestamp`
- mark delivered on `2xx`
- retry with exponential-ish delay otherwise

**Step 4: Wire worker**

In `worker.py`, after job queue work or when queue idle, call `outbox.deliver_once(...)`. Keep existing ingestion jobs working.

**Step 5: Run tests**

Run:

```bash
uv run pytest tests/integration/test_worker.py tests/test_signature.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/outbox.py src/brain/worker.py tests/integration/test_worker.py tests/test_signature.py
git commit -m "feat(brain): entrega eventos para Hermes"
```

---

### Task 9: Raw Agent Note Lifecycle Tools

**Files:**
- Modify: `brain/src/brain/mcp/handlers.py`
- Modify: `brain/src/brain/mcp/server.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`

**Step 1: Write failing tests**

Tests:

- curator can list pending notes
- curator can get raw note content
- clients cannot list/get raw notes
- `claim_agent_note` is optional and changes pending to in_review
- `complete_agent_note` works from pending or in_review
- `reject_agent_note` works from pending or in_review
- `fail_agent_note` stores error
- `complete_agent_note` stores flexible outcome

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -v
```

Expected: FAIL.

**Step 3: Implement handlers**

Add:

```python
async def list_agent_notes(deps, status=None, client_slug=None, limit=50, cursor=None)
async def get_agent_note(deps, note_id)
async def claim_agent_note(deps, note_id)
async def complete_agent_note(deps, note_id, outcome=None)
async def reject_agent_note(deps, note_id, reason=None)
async def fail_agent_note(deps, note_id, error=None)
```

`get_agent_note` reads the Markdown file from repo cache path and returns metadata + `content`.

**Step 4: Register tools**

Add all six tools to `server.py`.

**Step 5: Run tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/mcp/handlers.py src/brain/mcp/server.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): adiciona ciclo de vida de notas brutas"
```

---

### Task 10: Curated Note Create/Update/Get and Vault Tree

**Files:**
- Modify: `brain/src/brain/mcp/handlers.py`
- Modify: `brain/src/brain/mcp/server.py`
- Modify: `brain/src/brain/ingestion/pipeline.py`
- Modify: `brain/src/brain/storage/repositories.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`
- Test: `brain/tests/integration/test_pipeline.py`

**Step 1: Write failing tests**

Tests:

- `create_note` writes `projetos/brain.md`, creates parent dirs, indexes document, and returns id/path
- `create_note` rejects paths under `_agents/`
- `create_note` fails if path exists
- `update_note` replaces entire Markdown and reindexes
- `get_note` returns only curated notes
- `get_note("_agents/...")` is forbidden/not found
- `list_vault_tree` returns directories and notes, excluding `_agents/` by default

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py tests/integration/test_pipeline.py -v
```

Expected: FAIL.

**Step 3: Make indexing safe for optional LLM**

Current `pipeline.index_document` requires `llm` for entity extraction. Adjust so tests and note writes can index chunks even when `llm is None`:

```python
if llm is not None:
    ents = await extract_entities(llm, content)
    ...
```

Keep graph extraction when LLM exists.

**Step 4: Implement curated note handlers**

Add:

```python
async def create_note(deps, path, content, metadata=None, source_agent_note_ids=None)
async def update_note(deps, id_or_path, content, metadata=None, source_agent_note_ids=None)
async def get_note(deps, id_or_path)
async def list_vault_tree(deps, prefix=None, include_agents=False, max_depth=None)
```

Behavior:

- curator required for create/update/list tree
- client or curator allowed for `get_note`
- write via `git_writer.write_curated_note`
- frontmatter controlled by brain
- index via `pipeline.index_document(... namespace="curated", repo_path=path, ...)`
- preserve existing document id on update

**Step 5: Register tools**

Add tools to `server.py`.

**Step 6: Run tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py tests/integration/test_pipeline.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/brain/mcp/handlers.py src/brain/mcp/server.py src/brain/ingestion/pipeline.py src/brain/storage/repositories.py tests/integration/test_mcp_handlers.py tests/integration/test_pipeline.py
git commit -m "feat(brain): adiciona notas curadas"
```

---

### Task 11: Search Curated Knowledge Only

**Files:**
- Modify: `brain/src/brain/search/retriever.py`
- Modify: `brain/src/brain/storage/repositories.py`
- Modify: `brain/src/brain/mcp/handlers.py`
- Test: `brain/tests/integration/test_retriever.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`

**Step 1: Write failing tests**

Tests:

- `search` returns chunks from curated notes
- `search` does not return memories
- `search` does not return documents under `_agents/`
- optional `filters={"path_prefix": "projetos/"}` limits results
- `get_note` opens a result returned by search

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_retriever.py tests/integration/test_mcp_handlers.py -v
```

Expected: FAIL because current search includes memories.

**Step 3: Update repository search**

Change `search_chunks` to accept `filters` and exclude `_agents/%`:

```python
stmt = stmt.where(~Document.repo_path.startswith("_agents/"))
if filters and filters.get("path_prefix"):
    stmt = stmt.where(Document.repo_path.startswith(filters["path_prefix"]))
```

Do not include `search_memories` in the main retriever anymore.

**Step 4: Update retriever/handler signatures**

`search(query, limit=10, filters=None)` should be the public path. Keep `include_graph` only if already needed by tests, but do not expose raw notes or memories.

**Step 5: Run tests**

Run:

```bash
uv run pytest tests/integration/test_retriever.py tests/integration/test_mcp_handlers.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/search/retriever.py src/brain/storage/repositories.py src/brain/mcp/handlers.py tests/integration/test_retriever.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): limita busca a notas curadas"
```

---

### Task 12: Obsidian Link Extraction and Resolution

**Files:**
- Create: `brain/src/brain/notes/__init__.py`
- Create: `brain/src/brain/notes/links.py`
- Modify: `brain/src/brain/mcp/handlers.py`
- Modify: `brain/src/brain/mcp/server.py`
- Modify: `brain/src/brain/storage/repositories.py`
- Test: `brain/tests/test_note_links.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`

**Step 1: Write failing unit tests for parser**

Cases:

```python
def test_extract_obsidian_links():
    links = extract_obsidian_links("[[MCP]] [[projetos/brain|Brain]] [[Hermes#Curadoria]]")
    assert links == [
        {"target": "MCP", "alias": None, "anchor": None, "raw": "[[MCP]]"},
        {"target": "projetos/brain", "alias": "Brain", "anchor": None, "raw": "[[projetos/brain|Brain]]"},
        {"target": "Hermes", "alias": None, "anchor": "Curadoria", "raw": "[[Hermes#Curadoria]]"},
    ]
```

**Step 2: Write failing integration tests**

Tests:

- `create_note` extracts unresolved links into `note_links`
- exact path link resolves automatically when target exists
- `list_unresolved_links` returns unresolved links
- `resolve_note_link` requires existing non-`_agents` target
- clients cannot call link resolution tools

**Step 3: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_note_links.py tests/integration/test_mcp_handlers.py -v
```

Expected: FAIL.

**Step 4: Implement parser**

Use regex for `[[...]]`, split alias on first `|`, split anchor on first `#`.

**Step 5: Wire extraction**

After `create_note`/`update_note` indexes the document:

1. Extract links from content.
2. Try to resolve `target_path`:
   - if target ends with `.md` and document exists, resolve it
   - if target path + `.md` exists, resolve it
   - exact title matching can wait
3. `replace_note_links`.

**Step 6: Add handlers/tools**

```python
async def list_unresolved_links(deps, limit=50, cursor=None)
async def resolve_note_link(deps, link_id, target_path)
```

Register both in `server.py`.

**Step 7: Run tests**

Run:

```bash
uv run pytest tests/test_note_links.py tests/integration/test_mcp_handlers.py -v
```

Expected: PASS.

**Step 8: Commit**

```bash
git add src/brain/notes tests/test_note_links.py src/brain/mcp/handlers.py src/brain/mcp/server.py src/brain/storage/repositories.py tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): extrai e resolve links entre notas"
```

---

### Task 13: Remove remember and Old Public Memory Tools

**Files:**
- Modify: `brain/src/brain/mcp/server.py`
- Modify: `brain/src/brain/mcp/handlers.py`
- Modify: `brain/src/brain/search/retriever.py`
- Test: `brain/tests/integration/test_mcp_handlers.py`
- Test: `brain/tests/integration/test_retriever.py`

**Step 1: Write failing tests for removed behavior**

Update old tests:

- remove/replace `test_remember_grava_nota_e_enfileira`
- assert `create_mcp_server` does not expose `remember` if there is a stable way to inspect tools; otherwise direct handler tests are enough
- memory CRUD handler tests can be removed or left internal only, but not registered as MCP tools

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py tests/integration/test_retriever.py -v
```

Expected: FAIL while old tools are present or old tests expect them.

**Step 3: Remove public MCP registrations**

Remove from `server.py`:

- `remember`
- `get_memory`
- `list_memories`
- `update_memory`
- `move_memory`
- `delete_memory`
- `merge_memories`
- namespace tools if no longer useful for the new design

Keep entity tools only if `search`/curation still need them; otherwise leave for a later cleanup.

**Step 4: Remove or demote handlers**

Remove `remember` handler. Keep lower-level memory repository code for now if migrations/tests still cover it. Do not remove tables in this task.

**Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/integration/test_mcp_handlers.py tests/integration/test_retriever.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/brain/mcp/server.py src/brain/mcp/handlers.py tests/integration/test_mcp_handlers.py tests/integration/test_retriever.py
git commit -m "refactor(brain): remove remember da superficie MCP"
```

---

### Task 14: GitHub Webhook and Indexing Guards

**Files:**
- Modify: `brain/src/brain/main.py`
- Modify: `brain/src/brain/worker.py`
- Test: `brain/tests/integration/test_main.py`
- Test: `brain/tests/integration/test_worker.py`

**Step 1: Write failing tests**

Tests:

- GitHub webhook ignores `_agents/...` changes
- GitHub webhook indexes non-`_agents` markdown as curated notes
- worker does not process `_agents/...` as curated document if a job appears accidentally

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_main.py tests/integration/test_worker.py -v
```

Expected: FAIL if `_agents` is enqueued/indexed.

**Step 3: Implement guards**

In webhook changed-file loop:

```python
if path == "_agents" or path.startswith("_agents/"):
    continue
```

For non-agent markdown, use curated namespace or metadata, not `path.split("/")[0]` as a trust boundary.

In worker document handling:

```python
if p["repo_path"].startswith("_agents/"):
    raise ValueError("agent notes are not indexed as curated documents")
```

**Step 4: Run tests**

Run:

```bash
uv run pytest tests/integration/test_main.py tests/integration/test_worker.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/brain/main.py src/brain/worker.py tests/integration/test_main.py tests/integration/test_worker.py
git commit -m "fix(brain): ignora inbox bruta na indexacao"
```

---

### Task 15: Documentation and Example Instructions

**Files:**
- Modify: `brain/README.md`
- Modify: `brain/.env.example`
- Test: `brain/tests/test_config.py`

**Step 1: Write/update docs**

Document:

- new env vars
- curator bootstrap
- create client flow
- configuring a client with its token
- client tools
- Hermes tools
- `_agents/` vs curated notes
- webhook to Hermes

**Step 2: Update `.env.example`**

Add:

```env
BRAIN_CURATOR_SLUG=hermes
BRAIN_CURATOR_NAME=Hermes
BRAIN_CURATOR_TOKEN=...
BRAIN_TOKEN_ENCRYPTION_KEY=...
HERMES_WEBHOOK_URL=
HERMES_WEBHOOK_SECRET=
```

Include a comment that the encryption key must be a Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Step 3: Run config/docs-adjacent tests**

Run:

```bash
uv run pytest tests/test_config.py tests/test_auth.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add README.md .env.example tests/test_config.py
git commit -m "docs(brain): documenta fluxo de inbox e curadoria"
```

---

### Task 16: Full Verification

**Files:**
- No source edits unless verification reveals issues.

**Step 1: Run full suite**

Run:

```bash
uv run pytest
```

Expected: PASS.

**Step 2: Run diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected:

- no whitespace errors
- only intentional uncommitted changes, ideally none after commits

**Step 3: Review MCP surface**

Manually inspect `src/brain/mcp/server.py` and verify registered tools match:

Client:

```text
search
get_note
submit_agent_note
```

Hermes:

```text
create_agent_client
list_agent_clients
get_agent_client
reveal_agent_client_token
rotate_agent_client_token
disable_agent_client
list_agent_notes
get_agent_note
claim_agent_note
complete_agent_note
reject_agent_note
fail_agent_note
list_vault_tree
create_note
update_note
get_note
search
list_unresolved_links
resolve_note_link
```

**Step 4: Commit final fixes if needed**

If any verification fix is needed:

```bash
git add <files>
git commit -m "fix(brain): ajusta verificacao final da inbox"
```

**Step 5: Report**

Report:

- final test result
- branch name
- key behavior changes
- any remaining migration/deployment notes
