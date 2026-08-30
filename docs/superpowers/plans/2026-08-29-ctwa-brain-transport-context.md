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
- Correlation is by exact `message_id` join and is fail-closed: any identifier that does not resolve leaves the turn `pending`, then `uncorrelatable` after the grace period. No nearest-time or best-guess fallback.
- Correlation is re-evaluated, never computed once. A turn registered before its events are ingested must still correlate when they arrive (spec premise P6).
- `pre_gateway_dispatch` performs no I/O of any kind. It runs unbounded upstream, so blocking there wedges inbound dispatch for every message (spec premise P4).
- All three Kanban stage cards for one lead carry the same origin turn; an internal re-invocation never becomes or replaces an origin turn (spec 10.1.1).
- Plan 1 owns transport facts and correlation, not lifecycle interpretation. `transport_kind` is `ctwa_candidate|ordinary_inbound`; `inbound_kind` is lifecycle-relative and remains `null` until an exact lifecycle binding exists.
- Never infer lifecycle origin or semantics from contact-global chronology. A historical CTWA candidate for the same phone may belong to another lifecycle/campaign.
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

### Task 4: Register Hermes turns and correlate by exact message identifier

> **Amended 2026-08-30 (spec Amendment 1).** The originally shipped version of this task correlated by body HMAC over a debounce window and computed the result once, at registration. Spec premise P6 proved that non-deterministic in production. This task now describes the target contract; the code in `src/brain/turn_correlation.py` still implements the superseded one and must be migrated.

**Files:**
- Modify: `src/brain/turn_correlation.py`
- Modify: `src/brain/gateway_api.py`
- Modify: `src/brain/transport_service.py`
- Modify: `src/brain/service.py`
- Modify: `tests/test_turn_correlation.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- `POST /internal/gateway/turn-register`, gateway capability `turn_register`.
- Input: trusted gateway session context + upstream Hermes `turn_id` + current `user_message` + timestamp + `message_ids` (ordered, possibly empty).
- Raw `user_message` and raw `message_ids` are used only in memory, to derive `body_hmac`/length and candidate `event_id` values. Neither is stored or logged.
- Output: `status`, `wa_turn_id`, `correlation=correlated|pending|uncorrelatable`.
- `ambiguous` is no longer reachable from this route; exact-identifier correlation cannot produce it. The `conversation_context()` contract keeps the reason string (spec 8.4).

**Schema.** `whatsapp_turns.correlation_status` has no `CHECK` constraint, so `uncorrelatable` is storable today, and `transport_events.event_id` is the primary key, so a resolved identifier is a direct lookup needing no index.

One addition **is** required, and an earlier revision of this task wrongly claimed none was. Re-evaluating a `pending` turn means remembering which identifiers it is waiting for, and Brain can recover that information no other way:

- the raw `message_id` may not be persisted (spec 6.4 and this plan's Global Constraints);
- it cannot be recomputed later, because `event_id = HMAC(transport_secret, observer_device_id || key.id)` needs the raw `key.id`, which exists only during registration;
- it cannot be recovered from the arriving event either, because the observer sends Brain the already-derived `event_id` and never the raw message id.

Add `turn_candidate_events(wa_turn_id, ordinal, candidate_event_id)`, holding only derived HMAC identifiers, with no foreign key to `transport_events` precisely because the event may not exist yet. Rows are deleted once the turn reaches a terminal state.

**Observer identity must come from configuration.** Deriving a candidate requires the `observer_device_id`. Discovering it from existing `transport_events` fails for the first event on a fresh deployment, when none exist. Add `BRAIN_OBSERVER_DEVICE_IDS` to `BrainSettings` and derive candidates from the union of the configured identities and any device already seen, so a device rotation does not strand pending turns.

- [ ] **Step 1: Write exact-join tests**

For each supplied `message_id`, derive `event_id` with the observer's formula and require a direct hit. Cover the single-message turn and the multi-message debounce batch, asserting `turn_events.ordinal` follows dispatch order rather than timestamp order. Keep one embedded-newline case to prove the secondary body-HMAC consistency check still holds on the joined set.

Derive candidates using the observer device identities Brain knows from configuration and from `transport_events.observer_device_id`; never accept a device identity supplied in the request.

- [ ] **Step 2: Write arrival-order tests (the P6 regression)**

The decisive case: register the turn **before** the matching event is ingested, then ingest it. The turn must become `correlated` once the event arrives. Prove the reverse order still works, and that a duplicate ingestion does not duplicate `turn_events` rows.

This is the production failure the amendment exists to fix; it must fail before the fix and pass after.

- [ ] **Step 3: Write fail-closed and terminal-state tests**

An identifier that never resolves stays `pending` inside the grace period and becomes `uncorrelatable` after it. An identifier resolving to an event bound to a different contact is immediately `uncorrelatable`. A turn with an empty `message_ids` list is an internal re-invocation and creates **no** `whatsapp_turns` row. `uncorrelatable` is never re-evaluated. No nearest-time or best-guess fallback exists anywhere.

- [x] **Step 4: Decide the grace period explicitly**

**Decided 2026-08-30: 96 hours**, as the named setting `BRAIN_TURN_CORRELATION_GRACE_HOURS`, never a literal.

Derived, not estimated. `observers/whatsapp/src/main.mjs` sets `RETENTION_SECONDS = 72 * 60 * 60`: the observer keeps an unacknowledged event in its safe spool for 72 hours, redraining on every reconnect and restart, then purges it. An event that has not reached Brain within 72 hours therefore never will. The remaining 24 hours cover clock skew and the fact that purge is evaluated on scan rather than at the exact deadline.

Measuring typical ingestion latency was considered and rejected: it optimises an axis that costs nothing. The grace period delays no answer, because `conversation_context()` already reports `turn_not_correlated` for `pending`. It only decides when Brain stops re-evaluating.

The costs are asymmetric. Too short marks `uncorrelatable` — terminal and never re-evaluated — on an event that was still going to arrive after a Brain outage or observer reconnect, silently and permanently disabling lifecycle automation for that lead. Too long leaves some `pending` rows to be re-evaluated, which is cheap and well inside the 90-day transport retention of spec 19.

Two properties keep the generous value safe:

- The evidence-based rejection in spec 8.4 is immediate and independent of this timer. An identifier resolving to an event bound to a different contact is proof of impossibility and fails closed at once. The timer only covers the case with no evidence at all, where patience is correct.
- Detection of systemic breakage belongs to alerting, not to this timer. Spec 21 already requires an alert on unresolved correlation; a `pending` count and oldest-`pending` age surface a broken pipeline within minutes. Shortening the grace period to notice failures faster would trade real leads for an observability signal that already exists elsewhere. Never use a terminal state as a monitoring mechanism.

- [ ] **Step 5: Implement correlation and re-evaluation**

Authorize the Hermes session against `state.db`, resolve the Hermes chat to canonical phone, derive `contact_key`, derive each candidate `event_id`, and persist `whatsapp_turns`/`turn_events` only when every identifier resolves for that contact.

Re-evaluate `pending` turns on later transport ingestion for the same contact and on `conversation_context()`. Transport ingestion must stay durable if re-evaluation raises, matching the isolation Task 3 already requires.

- [ ] **Step 6: Migrate existing rows**

Rows registered under the superseded algorithm carry no identifiers and can never correlate. Sweep the `pending` ones to `uncorrelatable` once, and prove the sweep touches nothing already `correlated`.

- [ ] **Step 7: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_turn_correlation tests.test_transport_ingest tests.test_gateway_api -v
git add src/brain/turn_correlation.py src/brain/gateway_api.py src/brain/transport_service.py src/brain/service.py tests/test_turn_correlation.py tests/test_gateway_api.py
git commit -m "feat: correlate Hermes turns by exact message identifier"
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

> **Amended 2026-08-30 (spec Amendment 1).** Adds the `pre_gateway_dispatch` hook and changes `pre_tool_call` to key on the retained **origin turn** instead of the current turn. Spec premise P7 recorded that the shipped behaviour gave Cadastro and Reno cards `wa_turn_id` values from Kanban-notification turns, making the spec 10.3 binding unreachable.

**Interfaces:**
- One CEO model-visible native tool: `conversation_context({})` in `brain-context`.
- `pre_gateway_dispatch`: appends `event.message_id` to a bounded, TTL-expiring in-process buffer keyed by chat, and returns `None`. **Performs no network call, no disk I/O, and no blocking work of any kind** (spec 8.1, premise P4: upstream leaves this hook out of `_HOOK_TIMEOUT_BOUNDED_HOOKS`, so it runs unbounded and a slow dependency would wedge inbound dispatch for every message).
- `pre_llm_call`: drains the buffer for the current chat and registers the turn with those `message_ids` via `/internal/gateway/turn-register`. All network I/O lives here, where upstream applies a fail-open timeout.
- `pre_tool_call`: for default-profile WhatsApp `kanban_create` and assignee exactly `porteiro|cadastro|reno`, modifies `idempotency_key` to `whatsapp:<origin_wa_turn_id>:<assignee>`.
- `/internal/gateway/conversation-context`: reauthorizes the session/current registered turn and returns bounded context.

- [ ] **Step 1: Upgrade plugin tests to capture tools/hooks**

Assert exactly one model-visible tool `conversation_context` plus `pre_gateway_dispatch`, `pre_llm_call`, and `pre_tool_call` hooks. Do not expose CEO `conversation_phone` after migration.

Add a source-scanning regression test proving the `pre_gateway_dispatch` callback contains no network, filesystem, subprocess, or sleep call. Premise P4 makes this a safety property of the Hermes runtime, not a style preference, so it needs a test rather than a comment.

- [ ] **Step 2: Write contract/failure tests**

Success shape contains verified `contact.phone_e164`, optional display name/source, `turn.wa_turn_id`, and events ordered by `turn_events.ordinal`. Each event exposes only `event_id`, `transport_kind`, optional `source_app`, and `inbound_kind`. In Plan 1, `inbound_kind` is always `null`; no lifecycle-semantic or contact-chronology inference is permitted. Oversized/malformed/private-route failures return controlled `status=unavailable` to model boundary.

- [ ] **Step 3: Implement shared bounded localhost POST helper**

Keep all session context sourced at call time from official `gateway.session_context.get_session_env()`. Never accept model-supplied chat/session identity.

- [ ] **Step 4: Implement the message-identifier buffer**

Bounded per chat, TTL-expiring, guarded by the same lock discipline the current-turn map already uses. Premise P5 established that the gateway runs the agent turn in its own process, so this state is visible to `pre_llm_call`; a test must assert the buffer is drained exactly once per turn, so a retried or duplicated hook fire cannot replay identifiers into a later turn.

- [ ] **Step 5: Implement origin-turn retention and idempotency rewrite**

Retain the origin turn per chat, refreshing it **only** when the drained buffer is non-empty, which by premise P2 means an external WhatsApp turn. A Kanban-notification turn must leave the retained origin untouched, so a Cadastro or Reno card created during it still carries the originating turn.

When no origin turn is retained — TTL expired, or the gateway restarted mid-lead — leave the model-supplied key unchanged. Do not substitute the current internal turn: a wrong binding is silent and permanent, while an unrewritten key is still discoverable by the reconciler from `kanban.db`.

Never derive identity from message text or phone. Non-WhatsApp, non-default profile, unrelated tool, or an assignee outside the approved set is unchanged.

Tests must cover the full production sequence: external turn creates the Porteiro card, two notification turns follow, and all three cards carry one identical `wa_turn_id`.

- [ ] **Step 6: Run tests and commit**

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
TURN_EXACT_IDENTIFIER_JOIN=PASS
TURN_LATE_EVENT_RECORRELATES=PASS
TURN_UNCORRELATABLE_TERMINAL=PASS
TURN_INTERNAL_INVOCATION_NO_ROW=PASS
DISPATCH_HOOK_PERFORMS_NO_IO=PASS
CONVERSATION_CONTEXT_CONTRACT=PASS
KANBAN_IDEMPOTENCY_REWRITE=PASS
KANBAN_STAGES_SHARE_ORIGIN_TURN=PASS
HERMES_COMPATIBILITY_CHECK=PASS
HERMES_CORE_FILES_TOUCHED=NO
```
