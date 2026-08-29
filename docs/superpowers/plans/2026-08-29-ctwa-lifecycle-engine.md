# CTWA Lifecycle Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic Brain lifecycle engine that binds one proven CTWA origin to the exact Cadastro-created client, derives `Sem Atendimento` / `Não Respondeu` / `Em Atendimento` from durable facts, reconciles Hermes Kanban/delivery obligations read-only, and emits idempotent effects without writing FamaChat.

**Architecture:** Kanban idempotency keys reconstruct `wa_turn_id -> stage -> task`; Cadastro terminal run metadata proves the exact new `client_id`; Reno `response_ready` plus one unique Hermes `delivery_obligations.state=delivered` row proves first T1 send success; ordinary later transport events prove human inbound. Desired status is a pure function of facts. Writer claims contain a **transient** verified `expected_phone_e164` derived at claim time from lifecycle `contact_key` and current trusted WhatsApp mapping evidence; raw phone is never persisted in Brain runtime or logged.

**Tech Stack:** Python 3.11+, SQLite, standard-library JSON/HMAC/time, `unittest`, existing Brain `ReadOnlyDatabase` and Plan 1 runtime DB.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never write Hermes `state.db` or `kanban.db`; all evidence reads use the read-only abstraction.
- Add every newly queried Hermes table/column to compatibility schema guards first; missing schema disables lifecycle automation.
- Lifecycle only for terminal Cadastro decision `LEAD_NOVO_CADASTRADO` with one exact `client_id`, one bound `wa_turn_id`, one verified contact, and one proven `ctwa_first_contact` origin.
- `JA_E_CLIENTE`, `CORRETOR_ATIVO`, `INCONCLUSIVO`, failed Cadastro readback, ambiguity, or missing CTWA correlation creates no automated lifecycle.
- Facts are idempotent/immutable by lifecycle + fact type; ordering never controls final state.
- Desired status: human fact -> `Em Atendimento`; else T1-success -> `Não Respondeu`; else `Sem Atendimento`.
- Later `ctwa_attributed_inbound` never creates `first_human_inbound`.
- Allowed effects exactly: Sem→Não, Sem→Em, Não→Em.
- `brain.service` never owns the FamaChat status-write credential.
- Raw `response_ready`/ledger content/phone may be used transiently for proof/HMAC but are never copied into `brain-runtime.db` or logs.
- Claiming an effect requires current phone proof; failure to resolve the lifecycle contact yields no claim.
- All behavior is shadow-safe/restart-safe and follows TDD.

---

### Task 1: Extend read-only Hermes schema guards and evidence readers

**Files:**
- Modify: `src/brain/db.py`
- Create: `src/brain/hermes_evidence.py`
- Create: `tests/test_hermes_evidence.py`
- Modify: `tests/test_brain.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- Kanban `tasks` required: `id`, `assignee`, `status`, `current_run_id`, `session_id`, `idempotency_key`.
- `task_runs`: `id`, `task_id`, `status`, `summary`, `metadata`, `started_at`, `ended_at`.
- State `delivery_obligations`: `obligation_id`, `session_key`, `platform`, `chat_id`, `content`, `state`, `created_at`, `updated_at`.
- `HermesEvidenceReader.list_bound_tasks`, `terminal_run`, `delivered_obligations`.

- [ ] **Step 1: Write schema-guard RED tests**

Fixture DBs missing each required table/column must make compatibility fail; complete fixtures pass. No migration/DDL is ever run against Hermes DBs.

- [ ] **Step 2: Implement bounded typed evidence readers**

Parse run metadata only when it is a JSON object. Normalize timestamps through one helper. Terminal result requires task/run relationship and terminal/done status; malformed evidence returns controlled unavailable, not partial truth.

- [ ] **Step 3: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_hermes_evidence tests.test_brain tests.test_gateway_api -v
git add src/brain/db.py src/brain/hermes_evidence.py tests/test_hermes_evidence.py tests/test_brain.py tests/test_gateway_api.py
git commit -m "feat: read lifecycle evidence from Hermes safely"
```

---

### Task 2: Reconstruct Kanban bindings and create exact CTWA lifecycles

**Files:**
- Create: `src/brain/lifecycle_models.py`
- Create: `src/brain/lifecycle_engine.py`
- Create: `src/brain/reconcile.py`
- Create: `tests/test_lifecycle_binding.py`
- Modify: `src/brain/service.py`

**Interfaces:**
- `parse_whatsapp_idempotency_key(value) -> (wa_turn_id, stage) | None`, stages only `porteiro|cadastro|reno`.
- `LifecycleEngine.bind_completed_cadastro(task, run) -> BindResult`.
- `lead_lifecycles`: lifecycle ID, origin event ID, wa_turn_id, contact_key, client_id, phase, last proven status, timestamps.

- [ ] **Step 1: Write strict idempotency parser tests**

Accept only `whatsapp:waturn_<safe-id>:<approved-stage>`. Reject `unavailable`, phone/message-based keys, extra separators, unknown stages, and arbitrary task body text.

- [ ] **Step 2: Write lifecycle-binding tests**

Seed one correlated CTWA origin and Cadastro run metadata such as:

```python
{
  "status": "completed",
  "decision": "LEAD_NOVO_CADASTRADO",
  "entities": {"client_id": 12800},
  "response_ready": None,
  "requested_next_action": "return_to_ceo",
}
```

Require one lifecycle. Replay = no-op. Same origin→different client = hard binding conflict. `JA_E_CLIENTE`/`INCONCLUSIVO` = zero lifecycle.

- [ ] **Step 3: Implement one exact structured metadata parser**

Accept positive client ID only from the deployed structured `entities` shape verified in fixture. Do not parse the 200-character notification line when structured metadata exists.

- [ ] **Step 4: Materialize already-arrived human fact during bind**

Scan same-contact events after origin. First `ordinary_inbound` creates `first_human_inbound`; CTWA candidate/attributed event does not.

- [ ] **Step 5: Add restart-safe Kanban watermark**

Process terminal runs ascending by run ID; binding/lifecycle/watermark update is transactional.

- [ ] **Step 6: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_binding -v
git add src/brain/lifecycle_models.py src/brain/lifecycle_engine.py src/brain/reconcile.py src/brain/service.py tests/test_lifecycle_binding.py
git commit -m "feat: bind CTWA lifecycles to Cadastro clients"
```

---

### Task 3: Materialize human-inbound facts from later transport events

**Files:**
- Modify: `src/brain/transport_service.py`
- Modify: `src/brain/lifecycle_engine.py`
- Modify: `src/brain/reconcile.py`
- Create: `tests/test_lifecycle_human_inbound.py`

**Interfaces:**
- `observe_transport_event(event_id)` creates `first_human_inbound` at most once.

- [ ] **Step 1: Write ordering/dedup tests**

Cover ordinary event after origin, duplicate ordinary event, second CTWA-attributed event, ordinary event before lifecycle bind, and multiple active lifecycle safety.

- [ ] **Step 2: Implement exact active-lifecycle lookup**

Match contact_key, lifecycle `phase=active`, and event timestamp after origin. Only `ordinary_inbound` qualifies.

- [ ] **Step 3: Keep transport ingest independent of lifecycle callback failure**

Transport ACK remains durable if lifecycle observation raises; reconciliation repairs later.

- [ ] **Step 4: Add repair scan and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_human_inbound tests.test_transport_ingest -v
git add src/brain/transport_service.py src/brain/lifecycle_engine.py src/brain/reconcile.py tests/test_lifecycle_human_inbound.py
git commit -m "feat: derive human inbound lifecycle facts"
```

---

### Task 4: Prove first T1 send success from Reno + Hermes delivery ledger

**Files:**
- Modify: `src/brain/lifecycle_engine.py`
- Modify: `src/brain/reconcile.py`
- Create: `tests/test_lifecycle_delivery.py`

**Interfaces:**
- `match_first_t1_delivery(lifecycle, reno_run, obligations) -> DeliveryMatch`.
- Fact `first_t1_send_success` stores only technical evidence reference/observed timestamp.

- [ ] **Step 1: Write unique-match tests**

Exact Reno `metadata.response_ready` + same authorized session + compatible time + exactly one `delivery_obligations.state='delivered'` content match -> fact. Zero, pending/attempting/failed, wrong content/session/time, or multiple delivered matches -> no fact (`NOT_PROVEN`/ambiguous).

- [ ] **Step 2: Implement transient HMAC comparison**

Calculate response/ledger content HMAC in memory using the same Brain-safe body-HMAC helper; never persist/log raw text.

- [ ] **Step 3: Track delivery-retention risk**

Reconcile often enough for the supported ~7-day upstream ledger retention. Flag non-PII proof-at-risk if old eligible lifecycle lacks proof; never infer delivery after evidence expires.

- [ ] **Step 4: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_delivery -v
git add src/brain/lifecycle_engine.py src/brain/reconcile.py tests/test_lifecycle_delivery.py
git commit -m "feat: prove Reno T1 sends from Hermes ledger"
```

---

### Task 5: Derive desired state and superseding idempotent effects

**Files:**
- Modify: `src/brain/lifecycle_models.py`
- Modify: `src/brain/lifecycle_engine.py`
- Create: `tests/test_lifecycle_effects.py`

**Interfaces:**
- `desired_status(facts)`.
- `recompute_effects(lifecycle_id)`.
- States exactly `pending`, `claimed`, `applied`, `already_applied`, `superseded`, `conflict`, `retryable`, `permanent_failure`.

- [ ] **Step 1: Write pure state table tests**

```python
(set(), "Sem Atendimento")
({"first_t1_send_success"}, "Não Respondeu")
({"first_human_inbound"}, "Em Atendimento")
({"first_t1_send_success", "first_human_inbound"}, "Em Atendimento")
```

- [ ] **Step 2: Write effect/supersession tests**

Sem+T1 -> Sem→Não; Sem+human -> Sem→Em; pending Sem→Não then human -> old superseded, new Sem→Em; last proven Não + human -> Não→Em; no other transition produces an effect.

- [ ] **Step 3: Implement fact+effect calculation atomically**

Stable Brain-private HMAC effect ID from lifecycle/source/target/cause facts prevents duplicate effects across restarts.

- [ ] **Step 4: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_effects -v
git add src/brain/lifecycle_models.py src/brain/lifecycle_engine.py tests/test_lifecycle_effects.py
git commit -m "feat: derive idempotent lifecycle effects"
```

---

### Task 6: Expose writer claim/result API with transient contact proof, retention, and reconcile loop

**Files:**
- Create: `src/brain/lifecycle_api.py`
- Modify: `src/brain/mcp_server.py`
- Modify: `src/brain/service.py`
- Modify: `src/brain/reconcile.py`
- Modify: `src/brain/whatsapp_identity.py`
- Create: `tests/test_lifecycle_api.py`
- Create: `tests/test_retention.py`
- Modify: `tests/test_whatsapp_identity.py`
- Modify: `tests/test_deployment_contracts.py`

**Interfaces:**
- `POST /internal/lifecycle/claim`, writer service only.
- `POST /internal/lifecycle/result`, writer service only.
- Claim shape includes `effect_id`, lease token, client_id, expected/target status, cause, `mode`, plus **transient** `expected_phone_e164` resolved at claim time; this phone is never stored/logged by Brain or writer.
- `phone_for_contact_key(contact_key, hermes_mapping_dir, transport_secret)` enumerates only valid mapping evidence and requires exactly one canonical phone matching the HMAC contact key.

- [ ] **Step 1: Write contact-key reverse-resolution tests**

Valid mappings + contact_key -> exactly one phone. Missing/conflicting mappings -> unavailable. No database persistence of returned phone.

- [ ] **Step 2: Write claim/lease/result tests**

One writer claims once; concurrent second gets none; wrong principal/lease denied; lease expiry reclaimable; `applied`/`already_applied` update last proven status. If contact phone cannot be resolved at claim time, effect remains unclaimed and health exposes a non-PII blocked-contact count.

- [ ] **Step 3: Implement short-lived lease and private API**

Use random 128-bit lease token; never log it. Runtime mode defaults `shadow`. Claim does not imply write permission.

- [ ] **Step 4: Write retention tests**

Display name 24h; transport/turn attribution 90d; minimal active lifecycle survives transport purge; terminal lifecycle/effect audit +90d then purge.

- [ ] **Step 5: Implement `reconcile_once(now)`**

Order: Kanban binding/completion -> historical human repair -> delivery proof -> effect recompute -> retention. Per-lifecycle failures isolated; watermark advances only after durable processing.

- [ ] **Step 6: Extend health and run lifecycle suite**

No phone/name/client ID in health or metric labels.

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_binding tests.test_lifecycle_human_inbound tests.test_lifecycle_delivery tests.test_lifecycle_effects tests.test_lifecycle_api tests.test_retention tests.test_whatsapp_identity -v
git add src/brain tests/test_lifecycle_* tests/test_retention.py tests/test_whatsapp_identity.py tests/test_deployment_contracts.py
git commit -m "feat: complete Brain lifecycle shadow engine"
```

## Plan 3 Acceptance Gate

```text
KANBAN_BINDING_EXACT=PASS
CADASTRO_CLIENT_BINDING=PASS
LIFECYCLE_CONTACT_PHONE_PROOF=PASS
JA_E_CLIENTE_NO_LIFECYCLE=PASS
CORRETOR_ATIVO_NO_LIFECYCLE=PASS
EARLY_HUMAN_FACT=PASS
CTWA_ATTRIBUTED_NOT_HUMAN=PASS
T1_DELIVERED_UNIQUE_MATCH=PASS
FAILED_SEND_NO_T1_FACT=PASS
OUT_OF_ORDER_STATE_MACHINE=PASS
EFFECT_SUPERSESSION=PASS
RESTART_RECONCILIATION=PASS
RETENTION=PASS
FAMACHAT_WRITES=ZERO
HERMES_CORE_FILES_TOUCHED=NO
```
