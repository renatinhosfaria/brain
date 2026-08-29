# CTWA Brain Transport & Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Brain-owned runtime persistence, authenticated privacy-safe WhatsApp event ingestion, fail-closed turn correlation, `conversation_context()`, and CEO plugin hooks for stable `wa_turn_id`/Kanban idempotency without editing upstream Hermes Agent files.

**Architecture:** Brain keeps Hermes `state.db`/`kanban.db` read-only and owns `/var/lib/brain/runtime/brain-runtime.db`. The observer and Brain share one dedicated **transport HMAC key** used only to derive safe event/contact/body/opaque digests before an event reaches durable outbox storage. The Brain-only runtime HMAC key remains separate and derives `wa_turn_id`, effect IDs, leases, and other Brain-private identifiers. This separation lets the observer durably spool only allowlisted digests/metadata while preserving exact turn correlation after a Brain restart. `brain-ceo-bridge` uses official Hermes public hooks; it never patches upstream code.

**Tech Stack:** Python 3.11+, standard-library SQLite/HMAC/urllib, Starlette/uvicorn, MCP 2.0, `unittest`, Ruff, Hermes public plugin API (`register_tool`, `register_hook`).

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never edit, patch, monkeypatch, preload, wrap, or replace `/usr/local/lib/hermes-agent/**`.
- Hermes `state.db`, `kanban.db`, and Hermes WhatsApp session/mapping directory remain read-only to Brain.
- Brain runtime DB: `/var/lib/brain/runtime/brain-runtime.db`; only `brain.service` writes it.
- Observer mapping directory is independent: `/var/lib/brain/whatsapp-observer/session`.
- Brain binds only to localhost.
- `BRAIN_TRANSPORT_HMAC_SECRET` is a 32+ byte secret shared only by `brain.service` and `brain-whatsapp-observer.service`. It derives `event_id`, `contact_key`, `body_hmac`, `remote_jid_hmac`, and opaque CTWA HMACs. It never derives `wa_turn_id`.
- `BRAIN_RUNTIME_HMAC_SECRET` is Brain-only. It derives `wa_turn_id` and Brain-internal IDs.
- Raw WhatsApp message text, raw JID/LID, raw observer message ID, raw `sourceId`, raw `ctwaClid`, and full source URL are never persisted in Brain runtime or observer outbox.
- `event_id = "waevt_" + HMAC(transport_secret, observer_device_id || observer_message_id)`.
- `contact_key = HMAC(transport_secret, canonical_phone)`.
- `body_hmac = HMAC(transport_secret, message_body)`.
- `wa_turn_id = "waturn_" + HMAC(runtime_secret, hermes_turn_id)`.
- The observer service is trusted to derive the safe envelope because it necessarily sees raw transport data; Brain re-verifies contact identity against the observer mapping directory before accepting the event.
- Gateway/default capabilities are `turn_register` and `conversation_context`; worker fallback `conversation_phone` remains for Porteiro/Cadastro.
- Service principals are distinct and cannot use worker Task/Run identity as authority.
- Correlation is fail-closed: zero matches -> `turn_not_correlated`; multiple matches -> `ambiguous_transport_events`.
- A later event matching the proven CTWA detector is `ctwa_attributed_inbound`, never automatically `human_inbound`.
- Every behavior change follows TDD and every task ends with tests plus a focused commit.

---

### Task 1: Extend Brain settings and principal authorization

**Files:**
- Modify: `src/brain/config.py`
- Modify: `src/brain/authorization.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_service_authorization.py`
- Modify: `deploy/brain.toml.example`

**Interfaces:**
- `PrincipalConfig.mode`: `worker | gateway | service`.
- Brain capabilities: `conversation_recent`, `conversation_search`, `conversation_phone`, `turn_register`, `conversation_context`, `transport_ingest`, `lifecycle_claim`, `lifecycle_result`.
- `ServiceRequestIdentity(principal)` and `parse_service_headers(headers, required_capability)`.
- `parse_gateway_headers(headers, required_capability)` replaces the current phone-specific gateway gate.
- `BrainSettings` adds `runtime_db`, `observer_session_dir`, `runtime_hmac_secret`, `transport_hmac_secret`, `transport_retention_days=90`, `display_name_ttl_hours=24`.

- [ ] **Step 1: Write failing config tests**

Add tests that accept:

```python
principals={
    "default": principal("default", "gateway", "conversation_context", "turn_register"),
    "observer": principal("observer", "service", "transport_ingest"),
    "writer": principal("writer", "service", "lifecycle_claim", "lifecycle_result"),
}
```

and reject a short/missing stable runtime or transport HMAC secret in production settings.

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_config -v
```

Expected: service mode/settings unsupported.

- [ ] **Step 3: Implement minimal settings/ACL**

Use exact defaults:

```python
DEFAULT_RUNTIME_DB = Path("/var/lib/brain/runtime/brain-runtime.db")
DEFAULT_OBSERVER_SESSION_DIR = Path("/var/lib/brain/whatsapp-observer/session")
VALID_MODES = frozenset({"worker", "gateway", "service"})
VALID_TOOLS = frozenset({
    "conversation_recent", "conversation_search", "conversation_phone",
    "turn_register", "conversation_context", "transport_ingest",
    "lifecycle_claim", "lifecycle_result",
})
```

No unused `kanban_binding_register` capability is added.

- [ ] **Step 4: Prove mode/capability separation**

`tests/test_service_authorization.py` must prove observer can only `transport_ingest`, writer only claim/result, and worker/gateway tokens are rejected by service parser. Gateway parser must require the named capability and reject service tokens.

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_config tests.test_service_authorization -v
```

Expected: PASS.

- [ ] **Step 5: Update example config and commit**

Add server paths/retention and distinct principal examples; secrets are comments/env references, never checked-in values.

```bash
git add src/brain/config.py src/brain/authorization.py tests/test_config.py tests/test_service_authorization.py deploy/brain.toml.example
git commit -m "feat: add Brain runtime service principals"
```

---

### Task 2: Create Brain runtime SQLite and domain-separated IDs

**Files:**
- Create: `src/brain/runtime_db.py`
- Create: `src/brain/transport_models.py`
- Create: `tests/test_runtime_db.py`
- Modify: `src/brain/service.py`

**Interfaces:**
- `RuntimeDatabase.initialize/read/write`.
- `RuntimeIds(runtime_secret, transport_secret)` with `wa_turn_id`, `effect_id`, `contact_key`, `event_id`, `body_hmac`, `jid_hmac`, `opaque_hmac`.
- Tables: `transport_events`, `whatsapp_turns`, `turn_events`, `kanban_bindings`, `lead_lifecycles`, `lifecycle_facts`, `lifecycle_effects`, `contact_ephemera`, `reconcile_state`.

- [ ] **Step 1: Write schema and ID stability tests**

Assert exact table set, foreign keys, unique keys, WAL, and domain separation. Prove same observer/device message gives same `waevt_`, different device gives different ID, and `waturn_` never uses transport key.

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_db -v
```

- [ ] **Step 3: Implement SQLite/IDs**

Use HMAC-SHA256 with explicit domain prefixes and one transaction for schema initialization. Do not reuse the Hermes `ReadOnlyDatabase` class for Brain writes.

- [ ] **Step 4: Wire into `BrainService` and run regression tests**

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

### Task 3: Ingest the observer's safe event envelope

**Files:**
- Create: `src/brain/transport_api.py`
- Create: `src/brain/transport_service.py`
- Modify: `src/brain/mcp_server.py`
- Modify: `src/brain/whatsapp_identity.py`
- Modify: `src/brain/service.py`
- Create: `tests/test_transport_ingest.py`
- Modify: `tests/test_whatsapp_identity.py`

**Interfaces:**
- `POST /internal/transport/events`, service capability `transport_ingest`.
- Input contains only: `event_id`, `observer_device_id`, `received_at`, `message_timestamp`, `remote_jid_hmac`, optional derived `contact_key`, `body_hmac`, `body_length`, sanitized optional `display_name`, `native_type`, `transport_kind`, and already-sanitized CTWA metadata (`source_type`, `source_app`, source-id length/HMAC, URL hostname/length/HMAC, ctwaClid length/HMAC, booleans).
- No raw body/JID/message ID/source ID/ctwaClid/full URL is accepted by this endpoint.

- [ ] **Step 1: Add mapping-directory contact-key verification tests**

Extend `whatsapp_identity.py` with a helper that enumerates only valid allowlisted PN/LID mapping files, derives `contact_key`/`remote_jid_hmac` using the transport key, and requires exactly one canonical phone match. Conflicting/invalid mapping evidence is unavailable.

- [ ] **Step 2: Write ingestion RED tests**

Prove a safe CTWA envelope persists. Prove payloads containing forbidden raw fields (`body`, `remote_jid`, `message_id`, `sourceId`, `ctwaClid`, `sourceUrl`) are rejected. Prove duplicate `event_id` is a successful no-op. Prove unverified contact/JID HMAC returns retryable unavailable without persisting.

- [ ] **Step 3: Implement strict route**

Authenticate before body read, cap body size, validate exact field set/types, verify `event_id`/HMAC formats, independently match observer identity evidence through the observer mapping directory, and persist only the safe envelope. Display name is control-stripped/160-char bounded and gets an expiry timestamp.

- [ ] **Step 4: Run focused suite**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest tests.test_whatsapp_identity tests.test_gateway_api -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain/transport_api.py src/brain/transport_service.py src/brain/mcp_server.py src/brain/whatsapp_identity.py src/brain/service.py tests/test_transport_ingest.py tests/test_whatsapp_identity.py
git commit -m "feat: ingest privacy-safe WhatsApp transport events"
```

---

### Task 4: Register Hermes turns and correlate batched messages

**Files:**
- Create: `src/brain/turn_correlation.py`
- Modify: `src/brain/gateway_api.py`
- Modify: `src/brain/service.py`
- Create: `tests/test_turn_correlation.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- `POST /internal/gateway/turn-register`, gateway capability `turn_register`.
- Input: trusted gateway session context + upstream Hermes `turn_id` + current `user_message` + timestamp.
- Raw `user_message` is used only in memory to derive `body_hmac`/length and is never stored/logged.
- Output: `status`, `wa_turn_id`, `correlation=correlated|pending|ambiguous`.

- [ ] **Step 1: Write single-event, two-event debounce, and embedded-newline tests**

Seed safe transport events with `body_hmac`/length. Require the exact Hermes batching join (`"\n"`) to produce one unique ordered event combination. Include an event whose own body contains a newline so naive line-count matching fails and the implementation must test contiguous candidate partitions.

- [ ] **Step 2: Write fail-closed ambiguity tests**

Zero combinations -> `turn_not_correlated`; multiple exact combinations -> `ambiguous_transport_events`; no nearest-time fallback.

- [ ] **Step 3: Implement correlation**

Authorize the Hermes session against `state.db`, resolve the Hermes chat to canonical phone, derive `contact_key`, select candidate observer events in the supported debounce window, compare composed HMAC/length, and persist `whatsapp_turns`/`turn_events` only on unique proof.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_turn_correlation tests.test_gateway_api -v
git add src/brain/turn_correlation.py src/brain/gateway_api.py src/brain/service.py tests/test_turn_correlation.py tests/test_gateway_api.py
git commit -m "feat: correlate Hermes turns to WhatsApp events"
```

---

### Task 5: Expose `conversation_context()` and official CEO hooks

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
- One CEO model-visible native tool: `conversation_context({})` in `brain-context`.
- `pre_llm_call`: registers trusted current WhatsApp turn via `/internal/gateway/turn-register`.
- `pre_tool_call`: for default-profile WhatsApp `kanban_create` and assignee exactly `porteiro|cadastro|reno`, modifies `idempotency_key` to `whatsapp:<wa_turn_id>:<assignee>`.
- `/internal/gateway/conversation-context`: reauthorizes the session/current registered turn and returns bounded context.

- [ ] **Step 1: Upgrade plugin tests to capture tools/hooks**

Assert exactly one model-visible tool `conversation_context` plus `pre_llm_call` and `pre_tool_call` hooks. Do not expose CEO `conversation_phone` after migration.

- [ ] **Step 2: Write contract/failure tests**

Success shape contains verified `contact.phone_e164`, optional display name/source, `turn.wa_turn_id`, ordered events with only `event_id`, `inbound_kind`, optional `source_app`. Oversized/malformed/private-route failures return controlled `status=unavailable` to model boundary.

- [ ] **Step 3: Implement shared bounded localhost POST helper**

Keep all session context sourced at call time from official `gateway.session_context.get_session_env()`. Never accept model-supplied chat/session identity.

- [ ] **Step 4: Implement idempotency rewrite using the current registered `wa_turn_id`**

Use an in-process ContextVar keyed to the current hook turn or a Brain lookup by current upstream `turn_id`; never derive from message text/phone. Non-WhatsApp, non-default profile, unrelated tool, or other assignee is unchanged.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_ceo_bridge_plugin tests.test_gateway_api tests.test_turn_correlation -v
git add integrations/hermes/brain-ceo-bridge src/brain/gateway_api.py src/brain/service.py tests/test_ceo_bridge_plugin.py tests/test_gateway_api.py
git commit -m "feat: add trusted CEO conversation context"
```

---

### Task 6: Add health, compatibility, deployment examples, and full regression gate

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
- Health adds `runtime_db` and `hermes_compatibility` without PII.
- Compatibility checker verifies required public hooks/payload IDs, session ContextVars, read-only schemas, delivery-ledger semantics, and supported WhatsApp batching behavior; it never writes upstream.

- [ ] **Step 1: Add deployment-contract RED tests**

Require stable runtime/transport HMAC secrets, runtime/observer paths, default gateway capabilities `[conversation_context, turn_register]`, observer/writer principals, and CEO plugin expected tool `conversation_context`.

- [ ] **Step 2: Implement compatibility/health updates**

Unsupported Hermes means Brain lifecycle/context degraded/unavailable and writes disabled; Hermes service itself continues.

- [ ] **Step 3: Update docs/examples**

Document `/var/lib/brain/runtime` 0700, runtime DB 0600, secret separation, plugin deployment outside upstream, and rollback without upstream edits.

- [ ] **Step 4: Run full Brain quality gate**

```bash
uv run ruff check src tests scripts integrations
uv run ruff format --check src tests scripts integrations
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain/service.py deploy scripts tests/test_deployment_contracts.py README.md docs/runbook.md
git commit -m "chore: harden Brain transport context deployment"
```

## Plan 1 Acceptance Gate

```text
RUNTIME_DB_SCHEMA=PASS
PRINCIPAL_MODE_ACL=PASS
SAFE_TRANSPORT_ENVELOPE=PASS
OUTBOX_REQUIRES_NO_RAW_MESSAGE=PASS
TRANSPORT_INGEST_DEDUP=PASS
TRANSPORT_IDENTITY_REVERIFY=PASS
TURN_SINGLE_EVENT=PASS
TURN_BATCHED_EVENTS=PASS
TURN_AMBIGUITY_FAIL_CLOSED=PASS
CONVERSATION_CONTEXT_CONTRACT=PASS
KANBAN_IDEMPOTENCY_REWRITE=PASS
HERMES_COMPATIBILITY_CHECK=PASS
HERMES_CORE_FILES_TOUCHED=NO
```
