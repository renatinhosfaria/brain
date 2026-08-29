# CTWA Lifecycle Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic Brain lifecycle engine that binds a proven CTWA origin to the exact Cadastro-created client, derives `Sem Atendimento` / `Não Respondeu` / `Em Atendimento` from durable facts, reconciles Hermes Kanban and delivery obligations read-only, and emits idempotent lifecycle effects without writing FamaChat.

**Architecture:** Brain treats Hermes Kanban and delivery ledgers as read-only evidence and `brain-runtime.db` as its durable derived-state store. Kanban idempotency keys reconstruct `wa_turn_id -> stage -> task`; Cadastro run metadata proves the exact new `client_id`; Reno run `response_ready` plus a unique Hermes `delivery_obligations.state=delivered` row proves T1 send success. Ordinary non-CTWA transport events create the human-inbound fact. Desired CRM state is a pure function of facts, and effects are queued/superseded transactionally.

**Tech Stack:** Python 3.11+, SQLite, standard-library JSON/HMAC/time, `unittest`, existing Brain `ReadOnlyDatabase` and runtime DB.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never write Hermes `state.db` or `kanban.db`; all lifecycle evidence reads use the existing read-only database abstraction.
- Extend Brain's schema compatibility requirements before querying new Hermes columns/tables. Missing columns disable lifecycle compatibility; they never trigger a guessed fallback.
- Lifecycle is created only for terminal Cadastro decision `LEAD_NOVO_CADASTRADO` with one exact `client_id`, one bound `wa_turn_id`, and one proven `ctwa_first_contact` origin event.
- `JA_E_CLIENTE`, `CORRETOR_ATIVO`, Cadastro `INCONCLUSIVO`, failed readback, ambiguous run metadata, or missing CTWA correlation creates no automated lifecycle.
- Facts are immutable/upsert-idempotent by `(lifecycle_id, fact_type)`; arrival order must not affect desired state.
- Desired state formula is exact: human fact -> `Em Atendimento`; else T1-success fact -> `Não Respondeu`; else `Sem Atendimento`.
- A later `ctwa_attributed_inbound` never creates `first_human_inbound`.
- Allowed effects are exactly `Sem Atendimento -> Não Respondeu`, `Sem Atendimento -> Em Atendimento`, `Não Respondeu -> Em Atendimento`.
- No lifecycle effect is sent to FamaChat from `brain.service`.
- All new behavior is shadow-safe and restart-safe.

---

### Task 1: Extend Hermes read-only schema guards and evidence readers

**Files:**
- Modify: `src/brain/db.py`
- Create: `src/brain/hermes_evidence.py`
- Create: `tests/test_hermes_evidence.py`
- Modify: `tests/test_brain.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- Requires Kanban `tasks`: `id`, `assignee`, `status`, `current_run_id`, `session_id`, `idempotency_key`.
- Requires `task_runs`: `id`, `task_id`, `status`, `summary`, `metadata`, `started_at`, `ended_at`.
- Requires state `delivery_obligations`: `obligation_id`, `session_key`, `platform`, `chat_id`, `content`, `state`, `created_at`, `updated_at`.
- Produces `HermesEvidenceReader` with:
  - `list_bound_tasks(after_id: str|None) -> list[KanbanTaskEvidence]`
  - `terminal_run(task_id: str) -> KanbanRunEvidence | None`
  - `delivered_obligations(session_key: str, since: float) -> list[DeliveryEvidence]`

- [ ] **Step 1: Write schema-guard RED tests**

Create fixture DBs missing each newly required field/table and assert `SchemaGuard.check()` returns false. Add a compatible fixture with `idempotency_key`, run metadata, and `delivery_obligations` that returns true.

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_hermes_evidence tests.test_brain -v
```

- [ ] **Step 3: Extend `SCHEMA_REQUIREMENTS`**

Add only fields actually used by the lifecycle engine. Do not add DDL or migrations for Hermes DBs.

- [ ] **Step 4: Implement evidence reader with bounded queries**

Use dataclasses and JSON parsing that fails closed:

```python
@dataclass(frozen=True)
class KanbanRunEvidence:
    run_id: int
    task_id: str
    status: str
    metadata: dict[str, object]
    started_at: float | None
    ended_at: float | None
```

`terminal_run()` accepts only a terminal/done run and only JSON-object metadata; malformed metadata returns `None`/controlled reason, not partial data.

- [ ] **Step 5: Run tests and commit**

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
- Produces `parse_whatsapp_idempotency_key(value) -> (wa_turn_id, stage) | None` accepting only `whatsapp:waturn_<hex-or-base-safe>:porteiro|cadastro|reno`.
- Produces `LifecycleEngine.bind_completed_cadastro(task_evidence, run_evidence) -> BindResult`.
- `lead_lifecycles` stores `lifecycle_id`, `origin_event_id`, `wa_turn_id`, `contact_key`, `client_id`, `phase`, timestamps, and last proven FamaChat status.

- [ ] **Step 1: Write idempotency-key parser tests**

Test exact valid stages and reject missing prefixes, `unavailable`, phone-shaped keys, extra colons, unknown stages, and arbitrary task bodies.

- [ ] **Step 2: Write lifecycle-binding tests**

Seed `whatsapp_turns`/`turn_events` with one CTWA event and one optional ordinary event. Seed a Cadastro task/run with structured metadata:

```python
metadata = {
    "status": "completed",
    "decision": "LEAD_NOVO_CADASTRADO",
    "entities": {"client_id": 12800},
    "response_ready": None,
    "requested_next_action": "return_to_ceo",
}
```

Assert one lifecycle binds to `client_id=12800`. Re-running is a no-op. Same origin + different client is `LIFECYCLE_BINDING_CONFLICT`. `JA_E_CLIENTE` and `INCONCLUSIVO` create zero rows.

- [ ] **Step 3: Implement strict metadata extraction**

Accept `client_id` only as a positive integer (or digit string converted once) from the approved structured `entities` location. Do not parse notification text when structured metadata exists. If the current deployed Cadastro metadata shape differs, encode that exact shape in fixture tests before implementation and keep one parser path.

- [ ] **Step 4: Materialize early human fact on bind**

When lifecycle is created, scan already-correlated later events for the same `contact_key` and `received_at > origin_received_at`. The earliest `ordinary_inbound` creates `first_human_inbound`; `ctwa_candidate/ctwa_attributed_inbound` does not.

- [ ] **Step 5: Add Kanban reconcile watermark**

`reconcile_state` key `kanban_completed_watermark` tracks the last processed terminal run ID. Process in ascending run ID, transactionally updating bindings/lifecycles/watermark so restart is safe.

- [ ] **Step 6: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_binding -v
git add src/brain/lifecycle_models.py src/brain/lifecycle_engine.py src/brain/reconcile.py src/brain/service.py tests/test_lifecycle_binding.py
git commit -m "feat: bind CTWA lifecycles to Cadastro clients"
```

---

### Task 3: Materialize later human-inbound facts from transport ingestion

**Files:**
- Modify: `src/brain/transport_service.py`
- Modify: `src/brain/lifecycle_engine.py`
- Create: `tests/test_lifecycle_human_inbound.py`

**Interfaces:**
- Adds `LifecycleEngine.observe_transport_event(event_id: str) -> None`.
- Creates `lifecycle_facts.fact_type='first_human_inbound'` once per active lifecycle/contact.

- [ ] **Step 1: Write fact-order tests**

Cover:

```python
# CTWA origin then ordinary inbound
assert desired_status(lifecycle) == "Em Atendimento"

# CTWA origin then second CTWA-attributed event
assert fact_count("first_human_inbound") == 0

# duplicate ordinary event_id
assert fact_count("first_human_inbound") == 1
```

Also prove an ordinary inbound that happened before lifecycle binding is detected by Task 2's historical scan.

- [ ] **Step 2: Implement event-to-active-lifecycle lookup**

Lookup by `contact_key`, lifecycle `phase='active'`, and event timestamp after the origin event. Only `transport_kind='ordinary_inbound'` may create the fact.

- [ ] **Step 3: Call observation after durable transport ingest**

After the transport event transaction commits, invoke lifecycle observation. If lifecycle code fails, transport ingest must remain acknowledged/durable; reconciliation later repairs the fact.

- [ ] **Step 4: Add repair reconciliation**

Add a periodic scan for active lifecycles missing `first_human_inbound`, checking all ordinary events after origin so hook/callback failure is recoverable.

- [ ] **Step 5: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_human_inbound tests.test_transport_ingest -v
git add src/brain/transport_service.py src/brain/lifecycle_engine.py src/brain/reconcile.py tests/test_lifecycle_human_inbound.py
git commit -m "feat: derive human inbound lifecycle facts"
```

---

### Task 4: Prove first T1 send success from Reno metadata plus Hermes delivery ledger

**Files:**
- Modify: `src/brain/lifecycle_engine.py`
- Modify: `src/brain/reconcile.py`
- Create: `tests/test_lifecycle_delivery.py`

**Interfaces:**
- Produces `match_first_t1_delivery(lifecycle, reno_run, obligations) -> DeliveryMatch`.
- Creates immutable fact `first_t1_send_success` with evidence reference `obligation_id` and observed timestamp, but never stores raw `response_ready`.

- [ ] **Step 1: Write delivery matching tests**

Seed Reno run metadata containing exact `response_ready`. Seed obligations with same session key and content. Prove exactly one `state='delivered'` match creates the fact. Zero, `pending`, `attempting`, `failed`, different content, or two indistinguishable delivered rows create no fact and return `NOT_PROVEN`/ambiguous.

- [ ] **Step 2: Implement transient content HMAC matching**

Read `response_ready` only in memory, calculate `RuntimeIds.body_hmac(response_ready)`, and compare to HMAC calculated over each ledger `content` read in memory. Never copy raw content into `brain-runtime.db` or audit logs.

Require platform `whatsapp`, lifecycle-authorized `session_key`, and obligation created after the Reno run/turn window.

- [ ] **Step 3: Add delivery reconciliation watermark/risk alert state**

Reconcile active lifecycles frequently enough that the tested Hermes ~7-day retention cannot expire normally. Store `delivery_last_scan_at` and a non-PII `delivery_proof_at_risk` flag when an eligible lifecycle is old enough to approach retention without proof.

- [ ] **Step 4: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_delivery -v
git add src/brain/lifecycle_engine.py src/brain/reconcile.py tests/test_lifecycle_delivery.py
git commit -m "feat: prove Reno T1 sends from Hermes ledger"
```

---

### Task 5: Derive desired state and create/supersede lifecycle effects transactionally

**Files:**
- Modify: `src/brain/lifecycle_models.py`
- Modify: `src/brain/lifecycle_engine.py`
- Create: `tests/test_lifecycle_effects.py`

**Interfaces:**
- Produces `desired_status(facts) -> str`.
- Produces `recompute_effects(lifecycle_id) -> EffectDecision`.
- Effect states exactly: `pending`, `claimed`, `applied`, `already_applied`, `superseded`, `conflict`, `retryable`, `permanent_failure`.

- [ ] **Step 1: Write the state-machine table tests**

```python
cases = [
    (set(), "Sem Atendimento"),
    ({"first_t1_send_success"}, "Não Respondeu"),
    ({"first_human_inbound"}, "Em Atendimento"),
    ({"first_t1_send_success", "first_human_inbound"}, "Em Atendimento"),
]
```

- [ ] **Step 2: Write effect tests**

Prove:
- Sem + T1 -> one pending Sem→Não effect;
- Sem + human -> one pending Sem→Em effect;
- pending Sem→Não then human arrives -> old effect `superseded`, new Sem→Em effect pending;
- last proven `Não Respondeu` + human -> Não→Em effect;
- any source/target outside the three approved transitions -> no effect + controlled audit reason.

- [ ] **Step 3: Implement in one runtime DB transaction**

Fact insert, desired-status calculation, supersede, and new effect creation must be atomic. Give each effect a stable HMAC-derived `effect_id` from lifecycle/source/target/cause facts so restart/recompute cannot duplicate it.

- [ ] **Step 4: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_effects -v
git add src/brain/lifecycle_models.py src/brain/lifecycle_engine.py tests/test_lifecycle_effects.py
git commit -m "feat: derive idempotent lifecycle effects"
```

---

### Task 6: Expose writer claim/result APIs, shadow health, retention, and restart reconciliation

**Files:**
- Create: `src/brain/lifecycle_api.py`
- Modify: `src/brain/mcp_server.py`
- Modify: `src/brain/service.py`
- Modify: `src/brain/reconcile.py`
- Create: `tests/test_lifecycle_api.py`
- Create: `tests/test_retention.py`
- Modify: `tests/test_deployment_contracts.py`

**Interfaces:**
- `POST /internal/lifecycle/claim`: writer service auth only; returns at most one leased effect.
- `POST /internal/lifecycle/result`: writer service auth only; accepts exact `effect_id`, lease token, result enum, and proven final status.
- Lease expiry makes an unreported `claimed` effect eligible again.
- Runtime mode defaults `shadow`; claim returns `mode` and writer must not infer permission from target status alone.

- [ ] **Step 1: Write claim/lease/result tests**

Prove one writer claims once, second concurrent claim gets none, wrong token/principal is denied, expired lease is reclaimable, result with wrong lease token is rejected, and `applied/already_applied` updates `last_proven_status`.

- [ ] **Step 2: Implement API and service methods**

Use a random 128-bit lease token generated by Brain; store only its HMAC in DB if wire replay protection is desired, or store token as opaque non-PII technical state with short expiry. Never log it.

- [ ] **Step 3: Write retention tests with injectable clock**

Prove display-name ephemera purges after 24h, transport/turn attribution after 90d, active lifecycle minimal binding survives transport purge, terminal lifecycle/effect audit purges after +90d.

- [ ] **Step 4: Implement `reconcile_once(now)` orchestration**

Order: Kanban bindings/completions -> historical human repair -> delivery proof -> effect recompute -> retention. A failure in one lifecycle must not abort others; watermark advances only past durably processed rows.

- [ ] **Step 5: Extend health without PII**

Expose lifecycle mode, pending effect count, oldest pending age, delivery risk count, but no phone/name/client ID labels or values.

- [ ] **Step 6: Run full lifecycle suite and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_binding tests.test_lifecycle_human_inbound tests.test_lifecycle_delivery tests.test_lifecycle_effects tests.test_lifecycle_api tests.test_retention -v
git add src/brain tests/test_lifecycle_* tests/test_retention.py tests/test_deployment_contracts.py
git commit -m "feat: complete Brain lifecycle shadow engine"
```

## Plan 3 Acceptance Gate

Before implementing FamaChat writes, run the engine with real production evidence in **shadow** mode and require:

```text
KANBAN_BINDING_EXACT=PASS
CADASTRO_CLIENT_BINDING=PASS
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
