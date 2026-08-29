# CTWA Brain Transport & Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Brain-owned transport persistence, authenticated CTWA event ingestion, fail-closed WhatsApp turn correlation, `conversation_context()`, and the CEO plugin hooks needed to derive stable `wa_turn_id`/Kanban idempotency without editing upstream Hermes Agent files.

**Architecture:** Brain gains a writable runtime SQLite database separate from Hermes' read-only `state.db`/`kanban.db`. The external `brain-ceo-bridge` uses official Hermes plugin hooks to register each WhatsApp turn and enforce Kanban idempotency, while the zero-argument `conversation_context()` tool returns only the proven phone, sanitized display name, `wa_turn_id`, and correlated event classifications. Transport ingestion is authenticated by a new service principal and never persists raw message text, JID/LID, raw `sourceId`, raw `ctwaClid`, or full `contextInfo`.

**Tech Stack:** Python 3.11+, standard-library SQLite/HMAC/urllib, Starlette/uvicorn, MCP 2.0, `unittest`, Ruff, Hermes public plugin API (`register_tool`, `register_hook`).

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never edit, patch, monkeypatch, preload, wrap, or replace `/usr/local/lib/hermes-agent/**`.
- Hermes `state.db`, `kanban.db`, and the Hermes WhatsApp mapping directory remain read-only to Brain.
- Brain runtime DB is `/var/lib/brain/runtime/brain-runtime.db`; only `brain.service` opens it for writes.
- Observer mapping directory is independent from Hermes and defaults to `/var/lib/brain/whatsapp-observer/session`.
- Brain binds only to localhost.
- Raw WhatsApp message text may cross the authenticated localhost turn-registration boundary transiently, only to calculate HMAC/length/correlation; it is never stored in Brain runtime persistence or logs.
- Raw observer JID/LID may cross the authenticated localhost ingestion boundary transiently only so Brain can resolve it against the observer mapping directory; it is never stored in runtime persistence or logs.
- `event_id = "waevt_" + HMAC(runtime_secret, observer_device_id || observer_message_id)`.
- `wa_turn_id = "waturn_" + HMAC(runtime_secret, hermes_turn_id)`.
- The gateway/CEO principal exposes `conversation_context`; worker fallback `conversation_phone` remains unchanged for Porteiro/Cadastro.
- Service principals use distinct credentials and cannot present worker Task/Run headers as authority.
- Correlation is fail-closed: zero matches -> `turn_not_correlated`; multiple matches -> `ambiguous_transport_events`.
- A later event that itself matches the proven CTWA detector is `ctwa_attributed_inbound`, not `human_inbound`.
- Every behavior change follows TDD and every task ends in a focused test run plus a commit.

---

### Task 1: Extend Brain configuration and service-principal authorization

**Files:**
- Modify: `src/brain/config.py`
- Modify: `src/brain/authorization.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_service_authorization.py`
- Modify: `deploy/brain.toml.example`

**Interfaces:**
- Produces `PrincipalConfig.mode` values `worker | gateway | service`.
- Produces service capabilities `transport_ingest`, `turn_register`, `conversation_context`, `kanban_binding_register`, `lifecycle_claim`, `lifecycle_result` in Brain's internal allowlist; only the relevant capabilities are assigned to each principal.
- Produces `ServiceRequestIdentity(principal: str)` and `Authorizer.parse_service_headers(headers, required_capability) -> ServiceRequestIdentity`.
- Adds `BrainSettings.runtime_db: Path`, `observer_session_dir: Path`, `runtime_hmac_secret: bytes`, `transport_retention_days=90`, `display_name_ttl_hours=24`.

- [ ] **Step 1: Write failing configuration tests**

Add concrete tests to `tests/test_config.py`:

```python
def test_settings_accept_service_principals_and_runtime_paths(self):
    settings = BrainSettings(
        state_db=self.root / "state.db",
        kanban_db=self.root / "kanban.db",
        whatsapp_session_dir=self.root / "hermes-session",
        observer_session_dir=self.root / "observer-session",
        runtime_db=self.root / "runtime.db",
        runtime_hmac_secret=b"h" * 32,
        principals={
            "default": self._principal("default", "gateway", "g", "conversation_context"),
            "observer": self._principal("observer", "service", "o", "transport_ingest"),
            "writer": self._principal(
                "writer", "service", "w", "lifecycle_claim", "lifecycle_result"
            ),
        },
        cursor_secret=b"c" * 32,
    )
    self.assertEqual(settings.principals["observer"].mode, "service")
    self.assertEqual(settings.runtime_db, self.root / "runtime.db")
    self.assertEqual(settings.observer_session_dir, self.root / "observer-session")
    self.assertEqual(settings.runtime_hmac_secret, b"h" * 32)


def test_settings_rejects_short_runtime_hmac_secret(self):
    with self.assertRaises(ValueError):
        BrainSettings(
            principals={"default": self._principal("default", "gateway", "g", "conversation_context")},
            runtime_hmac_secret=b"short",
            cursor_secret=b"c" * 32,
        )
```

- [ ] **Step 2: Run config tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_config -v
```

Expected: failures for unknown `service` mode and missing runtime settings.

- [ ] **Step 3: Implement the minimal config model**

In `src/brain/config.py`, add constants and fields with exact defaults:

```python
DEFAULT_RUNTIME_DB = Path("/var/lib/brain/runtime/brain-runtime.db")
DEFAULT_OBSERVER_SESSION_DIR = Path("/var/lib/brain/whatsapp-observer/session")
VALID_MODES = frozenset({"worker", "gateway", "service"})
VALID_TOOLS = frozenset({
    "conversation_recent", "conversation_search", "conversation_phone",
    "conversation_context", "transport_ingest", "turn_register",
    "kanban_binding_register", "lifecycle_claim", "lifecycle_result",
})
```

Load `BRAIN_RUNTIME_DB`, `BRAIN_OBSERVER_SESSION_DIR`, `BRAIN_RUNTIME_HMAC_SECRET`, `BRAIN_TRANSPORT_RETENTION_DAYS`, and `BRAIN_DISPLAY_NAME_TTL_HOURS`. Require `runtime_hmac_secret` to be at least 32 bytes, exactly as `cursor_secret` is bounded today.

- [ ] **Step 4: Write and run service-auth tests**

Create `tests/test_service_authorization.py` proving:

```python
def test_observer_token_can_only_use_transport_ingest(self):
    ident = authorizer.parse_service_headers(
        {"Authorization": "Bearer observer-secret"}, "transport_ingest"
    )
    self.assertEqual(ident.principal, "observer")
    with self.assertRaises(BrainError):
        authorizer.parse_service_headers(
            {"Authorization": "Bearer observer-secret"}, "lifecycle_claim"
        )
```

Also prove worker and gateway tokens are rejected by `parse_service_headers`.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_config tests.test_service_authorization -v
```

Expected: PASS.

- [ ] **Step 5: Update deploy example and commit**

Update `deploy/brain.toml.example` with `[server]` runtime paths/secret comment and service principals:

```toml
runtime_db = "/var/lib/brain/runtime/brain-runtime.db"
observer_session_dir = "/var/lib/brain/whatsapp-observer/session"
transport_retention_days = 90
display_name_ttl_hours = 24
# runtime_hmac_secret = "generate-with-openssl-rand-hex-32"

[principals.default]
mode = "gateway"
tools = ["conversation_context"]

[principals.observer]
mode = "service"
tools = ["transport_ingest"]

[principals.writer]
mode = "service"
tools = ["lifecycle_claim", "lifecycle_result"]
```

Commit:

```bash
git add src/brain/config.py src/brain/authorization.py tests/test_config.py tests/test_service_authorization.py deploy/brain.toml.example
git commit -m "feat: add Brain runtime service principals"
```

---

### Task 2: Create the writable Brain runtime database and identifier primitives

**Files:**
- Create: `src/brain/runtime_db.py`
- Create: `src/brain/transport_models.py`
- Create: `tests/test_runtime_db.py`
- Modify: `src/brain/service.py`

**Interfaces:**
- Produces `RuntimeDatabase(path: Path, timeout_seconds: float)` with `initialize()`, `read(callback)`, and `write(callback)`.
- Produces `RuntimeIds(secret: bytes)` with `contact_key(phone)`, `event_id(observer_device_id, message_id)`, `wa_turn_id(turn_id)`, `body_hmac(text)`, and `opaque_hmac(value)`.
- Creates the spec tables `transport_events`, `whatsapp_turns`, `turn_events`, `kanban_bindings`, `lead_lifecycles`, `lifecycle_facts`, `lifecycle_effects`, `contact_ephemera`, and `reconcile_state` with foreign keys and unique constraints.

- [ ] **Step 1: Write the failing schema/idempotency tests**

Create `tests/test_runtime_db.py` with tests that open a temporary DB, call `initialize()`, and assert the exact table set. Add identifier stability tests:

```python
def test_runtime_ids_are_stable_and_domain_separated(self):
    ids = RuntimeIds(b"k" * 32)
    self.assertEqual(ids.event_id("observer-a", "MSG1"), ids.event_id("observer-a", "MSG1"))
    self.assertNotEqual(ids.event_id("observer-a", "MSG1"), ids.event_id("observer-b", "MSG1"))
    self.assertTrue(ids.event_id("observer-a", "MSG1").startswith("waevt_"))
    self.assertTrue(ids.wa_turn_id("turn-1").startswith("waturn_"))
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_db -v
```

Expected: import/module failures.

- [ ] **Step 3: Implement `RuntimeIds` and `RuntimeDatabase`**

Use HMAC-SHA256 with explicit domain prefixes so identifiers cannot collide across domains:

```python
def _hmac(self, domain: str, *parts: str) -> str:
    raw = "\0".join((domain, *parts)).encode("utf-8")
    return hmac.new(self.secret, raw, hashlib.sha256).hexdigest()


def event_id(self, observer_device_id: str, message_id: str) -> str:
    return "waevt_" + self._hmac("event", observer_device_id, message_id)[:32]
```

`RuntimeDatabase.initialize()` must enable WAL, `PRAGMA foreign_keys=ON`, and create schema inside one transaction. Do not reuse `ReadOnlyDatabase`; keep Hermes DB protections isolated in `db.py`.

- [ ] **Step 4: Initialize runtime DB from `BrainService` and run tests**

In `BrainService.__init__`, construct/initialize the runtime DB and `RuntimeIds`. Update tests that instantiate `BrainSettings` to supply temporary `runtime_db` and `runtime_hmac_secret` where necessary.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_db tests.test_brain tests.test_gateway_api -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain/runtime_db.py src/brain/transport_models.py src/brain/service.py tests/test_runtime_db.py tests/test_brain.py tests/test_gateway_api.py
git commit -m "feat: add Brain runtime persistence"
```

---

### Task 3: Add authenticated observer-event ingestion and privacy-preserving CTWA normalization

**Files:**
- Create: `src/brain/transport_api.py`
- Create: `src/brain/transport_service.py`
- Modify: `src/brain/mcp_server.py`
- Modify: `src/brain/service.py`
- Create: `tests/test_transport_ingest.py`

**Interfaces:**
- HTTP `POST /internal/transport/events`, service capability `transport_ingest`.
- Consumes one bounded JSON object with `observer_device_id`, `message_id`, `received_at`, `message_timestamp`, `remote_jid`, `push_name`, `body`, `native_type`, and a nested allowlisted `external_ad_reply` object.
- Produces `{ "status": "ok", "event_id": "waevt_...", "duplicate": bool }`.
- Persists `contact_key`, body HMAC/length, sanitized display-name ephemera, CTWA booleans/HMACs/hostname, and `transport_kind` only.

- [ ] **Step 1: Write ingestion tests before the route exists**

Create tests for normal CTWA, duplicate event, ordinary inbound, malformed payload, observer-token ACL, and persistence redaction. Example assertion:

```python
def test_ingest_ctwa_persists_no_raw_transport_identifiers(self):
    response = post_event(ctwa_fixture())
    self.assertEqual(response.status_code, 200)
    row = runtime_conn.execute("SELECT * FROM transport_events").fetchone()
    serialized = json.dumps(dict(row))
    self.assertNotIn("5534999772714", serialized)
    self.assertNotIn("123456789012345@lid", serialized)
    self.assertNotIn("ctwa-secret-value", serialized)
    self.assertEqual(row["transport_kind"], "ctwa_candidate")
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest -v
```

Expected: missing route/service failures.

- [ ] **Step 3: Implement strict parsing and CTWA detector**

Implement fixed maximum body size and exact field/type validation. Resolve `remote_jid` with existing `resolve_phone(remote_jid, settings.observer_session_dir)`; reject unresolved identity without persisting the raw JID. Sanitize display names by removing ASCII controls and limiting to 160 characters.

Implement the proven detector exactly:

```python
def is_historical_ctwa(ad: Mapping[str, object]) -> bool:
    return (
        ad.get("present") is True
        and ad.get("sourceType") == "ad"
        and (
            ad.get("clickToWhatsappCall") is True
            or bool(ad.get("ctwaClid"))
            or bool(ad.get("sourceId"))
        )
    )
```

Persist `sourceId`/`ctwaClid` only as present/length/HMAC and source URL only as hostname/length/HMAC.

- [ ] **Step 4: Add route, deduplication, and audit-safe errors**

Wire `Route("/internal/transport/events", ..., methods=["POST"])` in `mcp_server.py`. Authenticate before reading the request body. Unique `event_id` conflict is a successful duplicate no-op, never a second row.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest tests.test_gateway_api -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain/transport_api.py src/brain/transport_service.py src/brain/mcp_server.py src/brain/service.py tests/test_transport_ingest.py
git commit -m "feat: ingest privacy-safe WhatsApp transport events"
```

---

### Task 4: Register Hermes turns and correlate one turn to one-or-more transport events

**Files:**
- Create: `src/brain/turn_correlation.py`
- Modify: `src/brain/gateway_api.py`
- Modify: `src/brain/service.py`
- Create: `tests/test_turn_correlation.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- HTTP `POST /internal/gateway/turn-register`, gateway capability `turn_register`.
- Consumes trusted session context plus `turn_id`, `user_message`, and an RFC3339/epoch turn timestamp.
- Produces `{ "status": "ok", "wa_turn_id": "waturn_...", "correlation": "correlated|pending|ambiguous" }`.
- Produces `TurnCorrelationResult(status, event_ids, reason)`.

- [ ] **Step 1: Write single-event and batching correlation tests**

Seed runtime events with body HMACs and timestamps, then test:

```python
def test_two_observer_messages_match_one_hermes_debounce_turn(self):
    # event A body="primeira", event B body="segunda"
    result = correlate_turn(
        contact_key="contact_x",
        hermes_message="primeira\nsegunda",
        candidate_events=[event_a, event_b],
        ids=runtime_ids,
    )
    self.assertEqual(result.status, "correlated")
    self.assertEqual(result.event_ids, ("waevt_a", "waevt_b"))
```

Also test an individual message containing a newline by partitioning the Hermes message into all contiguous line groups for the exact candidate count; require exactly one HMAC sequence match.

- [ ] **Step 2: Write ambiguity tests**

Prove two valid combinations return `ambiguous_transport_events`, and zero valid combinations return `turn_not_correlated`. No nearest-time fallback is allowed.

- [ ] **Step 3: Run RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_turn_correlation -v
```

- [ ] **Step 4: Implement correlation and route**

Use the authorized Hermes session to resolve the Hermes-side phone with `settings.whatsapp_session_dir`, derive the same `contact_key`, select inbound candidate events in a bounded window around the turn, then match content partitions using HMAC/length. Persist `whatsapp_turns` and `turn_events` only for a unique proof. Store no raw `user_message`.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_turn_correlation tests.test_gateway_api -v
git add src/brain/turn_correlation.py src/brain/gateway_api.py src/brain/service.py tests/test_turn_correlation.py tests/test_gateway_api.py
git commit -m "feat: correlate Hermes turns to WhatsApp events"
```

---

### Task 5: Replace the CEO phone-only bridge with `conversation_context()` and turn hooks

**Files:**
- Modify: `integrations/hermes/brain-ceo-bridge/__init__.py`
- Modify: `integrations/hermes/brain-ceo-bridge/schemas.py`
- Modify: `integrations/hermes/brain-ceo-bridge/tools.py`
- Modify: `integrations/hermes/brain-ceo-bridge/plugin.yaml`
- Modify: `integrations/hermes/brain-ceo-bridge/README.md`
- Modify: `tests/test_ceo_bridge_plugin.py`
- Modify: `src/brain/gateway_api.py`
- Modify: `src/brain/service.py`

**Interfaces:**
- Native CEO tool `conversation_context({})` in toolset `brain-context`.
- `pre_llm_call` hook posts current trusted session + `turn_id` + current `user_message` to `/internal/gateway/turn-register`.
- `conversation_context()` posts trusted session + current `turn_id` to `/internal/gateway/conversation-context` and validates the bounded response shape.
- `pre_tool_call` hook for CEO `kanban_create` rewrites only `idempotency_key` when the assignee is exactly `porteiro`, `cadastro`, or `reno` and the active surface is default-profile WhatsApp DM.

- [ ] **Step 1: Upgrade plugin test context to capture tools and hooks**

Change the fake context to:

```python
class _Context:
    def __init__(self):
        self.tools = []
        self.hooks = []
    def register_tool(self, **kwargs):
        self.tools.append(kwargs)
    def register_hook(self, name, callback):
        self.hooks.append((name, callback))
```

Assert exactly one model-visible tool named `conversation_context`, plus registered `pre_llm_call` and `pre_tool_call` hooks.

- [ ] **Step 2: Write failing turn-registration/tool tests**

Test that `pre_llm_call(turn_id="turn-A", user_message="hello", platform="whatsapp", ...)` sends the trusted session context and never sends a model-provided `chat_id`. Test `conversation_context({})` accepts only shapes like:

```python
{
    "status": "ok",
    "contact": {"phone_e164": "5534999772714", "display_name": "Maria", "display_name_source": "whatsapp_profile"},
    "turn": {"wa_turn_id": "waturn_abc"},
    "events": [{"event_id": "waevt_abc", "inbound_kind": "ctwa_first_contact", "source_app": "instagram"}],
}
```

Malformed/oversized responses must become a controlled `status=unavailable` result.

- [ ] **Step 3: Implement tool and hook transport**

Refactor `tools.py` to a shared `_post_json(endpoint, payload, token)` helper with the existing 5-second timeout/16KiB response bound. Keep all gateway context sourced from `gateway.session_context.get_session_env()` at call time.

- [ ] **Step 4: Implement deterministic `pre_tool_call` idempotency rewrite**

For `kanban_create`, derive `wa_turn_id` from the current Hermes `turn_id` by calling a Brain private lookup or by using the same HMAC through the turn-registration response cached in a ContextVar local to the plugin. Return the official directive shape:

```python
return {
    "action": "modify",
    "args": {
        **args,
        "idempotency_key": f"whatsapp:{wa_turn_id}:{assignee}",
    },
}
```

Do not rewrite unrelated tools, non-WhatsApp sessions, non-default profiles, or assignees outside `{porteiro,cadastro,reno}`.

- [ ] **Step 5: Implement `/internal/gateway/conversation-context`**

Brain reauthorizes the session against `state.db`, requires the current registered/correlated `wa_turn_id`, resolves the verified phone, reads unexpired `contact_ephemera`, and returns events in arrival order. Event output is limited to `event_id`, `inbound_kind`, and optional `source_app`.

- [ ] **Step 6: Run focused plugin/gateway tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_ceo_bridge_plugin tests.test_gateway_api tests.test_turn_correlation -v
git add integrations/hermes/brain-ceo-bridge src/brain/gateway_api.py src/brain/service.py tests/test_ceo_bridge_plugin.py tests/test_gateway_api.py
git commit -m "feat: add trusted CEO conversation context"
```

---

### Task 6: Add health, compatibility, deployment examples, and regression coverage

**Files:**
- Modify: `src/brain/service.py`
- Modify: `deploy/brain.env.example`
- Modify: `deploy/brain.service`
- Modify: `deploy/hermes-ceo-brain.example.yaml`
- Modify: `scripts/hermes_integration_check.py`
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_deployment_contracts.py`
- Modify: `README.md`
- Modify: `docs/runbook.md`

**Interfaces:**
- Health adds `runtime_db` and `hermes_compatibility` fields while preserving controlled 503 behavior.
- Compatibility checker verifies public hook availability, `pre_llm_call` includes `turn_id`, `pre_tool_call` supports modify directives, Hermes session ContextVars still exist, and the supported batching contract is still present.

- [ ] **Step 1: Write deployment-contract tests**

Add tests asserting the new example has `runtime_db`, `observer_session_dir`, distinct observer/writer principals, and default gateway tool `conversation_context`. Assert the plugin doctor expectation changes from `{conversation_phone}` to `{conversation_context}`.

- [ ] **Step 2: Run deployment tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_deployment_contracts -v
```

- [ ] **Step 3: Update health and compatibility checks**

`hermes_integration_check.py` must read the installed Hermes source only; never write it. Fail compatibility when required public hook names/payload identifiers or WhatsApp batching semantics are absent. Do not fail the Hermes service itself: the checker is a rollout gate for Brain lifecycle features.

- [ ] **Step 4: Update deployment docs and examples**

Document `/var/lib/brain/runtime` mode `0700`, DB mode `0600`, the required stable HMAC secret, and the rollback rule: disabling the Brain plugin/observer must not require editing `/usr/local/lib/hermes-agent`.

- [ ] **Step 5: Run full Brain quality gate**

```bash
uv run ruff check src tests scripts integrations
uv run ruff format --check src tests scripts integrations
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain/service.py deploy scripts tests/test_deployment_contracts.py README.md docs/runbook.md
git commit -m "chore: harden Brain transport context deployment"
```

## Plan 1 Acceptance Gate

Before starting the observer plan, prove locally with fixtures:

```text
RUNTIME_DB_SCHEMA=PASS
SERVICE_PRINCIPAL_ACL=PASS
TRANSPORT_INGEST_DEDUP=PASS
TRANSPORT_PRIVACY=PASS
TURN_SINGLE_EVENT=PASS
TURN_BATCHED_EVENTS=PASS
TURN_AMBIGUITY_FAIL_CLOSED=PASS
CONVERSATION_CONTEXT_CONTRACT=PASS
KANBAN_IDEMPOTENCY_REWRITE=PASS
HERMES_CORE_FILES_TOUCHED=NO
```
