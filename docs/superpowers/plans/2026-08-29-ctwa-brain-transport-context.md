# CTWA Brain Transport & Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Brain-owned runtime persistence, authenticated privacy-safe WhatsApp event ingestion, enforced retention, and a contact-scoped `conversation_context()` served to the CEO through a hook-free plugin, without editing upstream Hermes Agent files.

**Architecture:** Brain keeps Hermes `state.db`/`kanban.db` read-only and owns `/var/lib/brain/runtime/brain-runtime.db`. The observer and Brain share one dedicated **transport HMAC key** used to derive every safe event/contact/body/opaque digest before an event reaches durable outbox storage. Amendment 2 removed the second, Brain-only domain along with the identifiers that used it. `brain-ceo-bridge` exposes one tool and registers no hooks; it never patches upstream code.

**Tech Stack:** Python 3.11+, standard-library SQLite/HMAC/urllib, Starlette/uvicorn, MCP 2.0, `unittest`, Ruff, Hermes public plugin API (`register_tool`, `register_hook`).

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never edit, patch, monkeypatch, preload, wrap, or replace `/usr/local/lib/hermes-agent/**`.
- Hermes `state.db`, `kanban.db`, and Hermes WhatsApp session/mapping directory remain read-only to Brain.
- Brain runtime DB: `/var/lib/brain/runtime/brain-runtime.db`; only `brain.service` writes it.
- Observer mapping directory is independent: `/var/lib/brain/whatsapp-observer/session`.
- Brain binds only to localhost.
- `BRAIN_TRANSPORT_HMAC_SECRET` is a 32+ byte secret shared only by `brain.service` and `brain-whatsapp-observer.service`. It derives `event_id`, `contact_key`, `body_hmac`, `remote_jid_hmac`, and opaque CTWA HMACs — every identifier Brain holds.
- `BRAIN_RUNTIME_HMAC_SECRET` is ignored since Amendment 2 and may be deleted from an installed env file. Keeping an unused key would advertise a separation that no longer exists.
- Raw WhatsApp message text, raw JID/LID, raw observer message ID, raw `sourceId`, raw `ctwaClid`, and full source URL are never persisted in Brain runtime or observer outbox.
- `event_id = "waevt_" + HMAC(transport_secret, observer_device_id || observer_message_id)`.
- `contact_key = HMAC(transport_secret, canonical_phone)`.
- `body_hmac = HMAC(transport_secret, message_body)`.
- The observer service is trusted to derive the safe envelope because it necessarily sees raw transport data; Brain re-verifies contact identity against the observer mapping directory before accepting the event.
- The gateway/default capability is `conversation_context`; worker fallback `conversation_phone` remains for Porteiro/Cadastro.
- Service principals are distinct and cannot use worker Task/Run identity as authority.
- Nothing in Brain performs network I/O inside a Hermes hook. The plugin registers no hooks at all, which is what makes a slow Brain unable to damage a turn.
- `pre_gateway_dispatch` performs no I/O of any kind. It runs unbounded upstream, so blocking there wedges inbound dispatch for every message (spec premise P4).
- All three Kanban stage cards for one lead carry the same origin turn; an internal re-invocation never becomes or replaces an origin turn (spec 10.1.1).
- This plan owns transport facts only. `transport_kind` is `ctwa_candidate|ordinary_inbound`; `inbound_kind` is always `null`, since nothing derives lifecycle-relative meaning any more.
- Never infer origin or semantics from contact-global chronology. A historical CTWA candidate for the same phone may belong to another campaign, which is why the context window and count are bounded.
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
- Brain capabilities: `conversation_recent`, `conversation_search`, `conversation_phone`, `conversation_context`, `transport_ingest`.
- `ServiceRequestIdentity(principal)` and `parse_service_headers(headers, required_capability)`.
- `parse_gateway_headers(headers, required_capability)` replaces the current phone-specific gateway gate.
- `BrainSettings` adds `runtime_db`, `observer_session_dir`, `transport_hmac_secret`, `transport_retention_days=90`, `display_name_ttl_hours=24`.

- [ ] **Step 1: Write failing config tests**

Add tests that accept:

```python
principals={
    "default": principal("default", "gateway", "conversation_context"),
    "observer": principal("observer", "service", "transport_ingest"),
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
    "conversation_context", "transport_ingest",
})
```

No unused `kanban_binding_register` capability is added.

- [ ] **Step 4: Prove mode/capability separation**

`tests/test_service_authorization.py` must prove the observer can only `transport_ingest` and that worker/gateway tokens are rejected by the service parser. Gateway parser must require the named capability and reject service tokens.

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

### Task 2: Create Brain runtime SQLite and derived IDs

**Files:**
- Create: `src/brain/runtime_db.py`
- Create: `src/brain/transport_models.py`
- Create: `tests/test_runtime_db.py`
- Modify: `src/brain/service.py`

**Interfaces:**
- `RuntimeDatabase.initialize/read/write`.
- `RuntimeIds(transport_secret)` with `contact_key`, `event_id`, `body_hmac`, `jid_hmac`, `opaque_hmac`.
- Tables: `transport_events` and `contact_ephemera`. Amendment 2 removed the other eight.

- [ ] **Step 1: Write schema and ID stability tests**

Assert the exact table set, unique keys and WAL. Prove the same observer/device message gives the same `waevt_`, a different device gives a different ID, and that changing the one transport secret moves every derived identifier.

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

### Task 4: Enforce retention on the ingestion path

> **Replaces the turn-correlation task, 2026-08-31 (spec Amendment 2).** Correlating a Hermes turn to an exact message identifier existed to make an automated CRM write exact. With that write descoped, the whole mechanism is gone: `wa_turn_id`, the dispatch identifier buffer, `whatsapp_turns`, `turn_events`, and the `pre_llm_call` registration that on 2026-08-31 destroyed an entire turn when Brain answered in 10.153 ms against a 5 s timeout.
>
> What this slot inherits is the one live obligation that was buried in the deleted code: section 19. Both of its limits were implemented only inside a reconciliation loop nothing ever called, so neither had ever been applied to production data.

**Files:**
- Modify: `src/brain/transport_service.py`
- Modify: `tests/test_transport_ingest.py`

**Step 1: Write the failing tests**
- [x] An expired display name is set to NULL in storage, not merely filtered out of reads.
- [x] A display name still inside its 24h window survives.
- [x] Transport events older than `transport_retention_days` are deleted; newer ones are not.
- [x] The first ingestion after start runs a pass, so policy is never waiting on a scheduler.
- [x] The periodic pass purges with no ingestion at all, driven by the real app lifespan.
- [x] A failed pass does not advance the throttle and is retried by the next trigger.
- [x] Passes are throttled, so ingestion cost stays bounded.
- [x] A failing retention pass never fails the ingestion that triggered it.

**Step 2: Implement**
- [x] Run the purge inside `TransportService.ingest`, after the durable write, throttled by `RETENTION_INTERVAL_SECONDS`.
- [x] Best-effort: log and continue on `sqlite3.Error`; the next event retries.

**Verification:**
- [x] `uv run ruff check src tests`
- [x] `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`

**Why two triggers:** ingestion is the only source of the data these limits govern, so a pass there can never be absent while the data grows — but it also stops when messages stop, and a quiet week would leave expired data on disk. A periodic pass bound to the app lifespan covers that, and being bound to the lifespan it starts with the service rather than needing a unit somebody must remember to install. A cron or a separate timer can be forgotten, mis-wired, or silently dead, which is precisely what happened to the previous implementation.

### Task 5: Serve `conversation_context()` scoped to the contact

> **Rewritten 2026-08-31 (spec Amendment 2).** The previous version registered three Hermes hooks and keyed context on a correlated turn. The plugin now registers none and the contract carries no turn: the CEO asks who is on the other side of this DM and whether they came from an ad, which is a property of the contact's recent transport.

**Files:**
- Modify: `integrations/hermes/brain-ceo-bridge/__init__.py`
- Modify: `integrations/hermes/brain-ceo-bridge/schemas.py`
- Modify: `integrations/hermes/brain-ceo-bridge/tools.py`
- Modify: `integrations/hermes/brain-ceo-bridge/plugin.yaml`
- Modify: `tests/test_ceo_bridge_plugin.py`
- Modify: `src/brain/gateway_api.py`
- Modify: `src/brain/service.py`

**Step 1: Write the failing tests**
- [x] The plugin registers exactly one tool and zero hooks, and no hook callable survives in the module.
- [x] The request body carries only session identity — no `wa_turn_id`, no `turn_id`.
- [x] The response is `{status, contact, events}` exactly; any extra field fails closed.
- [x] Events are transport evidence only: `inbound_kind` is null, and an asserted value is rejected.
- [x] Transport outside the window and beyond the count bound is excluded.
- [x] A Brain timeout returns a bounded `unavailable` and never raises into Hermes.

**Step 2: Implement**
- [x] Key the runtime query on `contact_key`, bounded by `CONTEXT_WINDOW_SECONDS` and `CONTEXT_MAX_EVENTS`.
- [x] Drop the `/internal/gateway/turn-register` route and its payload parser.

**Verification:**
- [x] `uv run ruff check src tests integrations`
- [x] `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`

**Bounds are the contract, not tuning:** a contact's full transport history is a profile, not context, and spec section 7 forbids reading a lifecycle origin out of the oldest CTWA event ever seen for a phone.

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

Require the stable transport HMAC secret, runtime/observer paths, the default gateway capability `[conversation_context]`, the observer principal, and the CEO plugin's single expected tool `conversation_context`.

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

## Acceptance Gate

```text
RUNTIME_DB_SCHEMA=PASS
PRINCIPAL_MODE_ACL=PASS
SAFE_TRANSPORT_ENVELOPE=PASS
OUTBOX_REQUIRES_NO_RAW_MESSAGE=PASS
TRANSPORT_INGEST_DEDUP=PASS
TRANSPORT_IDENTITY_REVERIFY=PASS
RETENTION_PURGES_EXPIRED_DISPLAY_NAME=PASS
RETENTION_PURGES_OLD_TRANSPORT=PASS
CONVERSATION_CONTEXT_CONTRACT=PASS
CONVERSATION_CONTEXT_IS_BOUNDED=PASS
PLUGIN_REGISTERS_NO_HOOKS=PASS
HERMES_COMPATIBILITY_CHECK=PASS
HERMES_CORE_FILES_TOUCHED=NO
```
