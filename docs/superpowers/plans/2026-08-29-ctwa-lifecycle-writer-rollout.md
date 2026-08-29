# CTWA Lifecycle Writer & Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the non-LLM lifecycle writer, prove whether FamaChat supports an atomic expected-current-status mutation, keep writes disabled until that proof passes, and provide integrity/shadow/go-live gates without editing upstream Hermes.

**Architecture:** `brain-lifecycle-writer.service` claims one precomputed effect from Brain, receives a transient verified `expected_phone_e164`, GETs the exact FamaChat client, proves client ID + phone equivalence + broker/source/current status, and defaults to dry-run. A separate safe capability probe inspects and tests `fc_patch_clientes_by_id` only on an operator-designated disposable client. Production write mode is impossible without a schema-bound PASS proof of server-side expected-state protection. Writer uses one proven mutation strategy, readbacks, and reports durable result to Brain.

**Tech Stack:** Python 3.11+, MCP 2.0 deterministic client, standard-library HTTP/JSON/hashlib, systemd, `unittest`, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- No LLM/model/provider code in writer.
- Never modify Hermes DBs/source/config/WhatsApp session.
- Writer Brain credential distinct from CEO/workers/observer.
- FamaChat write credential only `/etc/brain/writer.env` (0600); never model/Profile/Kanban/log/Git.
- Default `BRAIN_LIFECYCLE_WRITE_ENABLED=false`.
- Actual mutation requires **both** Brain claim `mode=write` and local write-enabled env plus valid atomic-proof artifact/fingerprint.
- Allowed transitions only Sem→Não, Sem→Em, Não→Em.
- Writer never changes broker/phone/name/source/notes/appointments or any other status.
- Before mutation prove: exact client ID; FamaChat phone equivalent to transient Brain `expected_phone_e164`; brokerId=35; source=`Facebook Ads`; current status exact expected source state. Already target -> `already_applied`; any other state/identity -> `conflict`.
- Phone comparison uses the approved normalization: digits only; remove country 55; exact compare; if one side 11 national digits and the other 10, remove the mobile ninth digit after DDD from the 11-digit side and compare. Never log either phone.
- GET→PATCH→GET without a proven server-side expected-state precondition is forbidden for production.
- No backfill; only new Brain lifecycles after rollout.
- Every task follows TDD + focused commit.

---

### Task 1: Capture exact FamaChat writer schemas and build a read-only writer

**Files:**
- Create: `scripts/capture_famachat_writer_schema.py`
- Create: `tests/test_famachat_writer_schema.py`
- Create: `tests/fixtures/famachat-writer-tools.json` (generated from non-secret live tools/list)
- Create: `src/brain/famachat_client.py`
- Create: `src/brain/lifecycle_writer.py`
- Create: `tests/test_famachat_client.py`
- Create: `tests/test_lifecycle_writer.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Schema capture performs MCP initialize/tools-list only and records exact non-secret input schemas for `fc_get_clientes_by_id` and `fc_patch_clientes_by_id`.
- `FamaChatClient.get_client(client_id)` uses the checked-in exact schema fixture, not a guessed envelope.
- `FamaChatClientRecord(client_id, phone, broker_id, status, source)`.
- `LifecycleWriter.run_once()` claims/validates one effect; zero write calls when write disabled.
- CLI entrypoint `brain-lifecycle-writer = brain.lifecycle_writer:main`.

- [ ] **Step 1: Implement/test read-only schema capture**

Output contains tool name, description, input schema, and SHA256 fingerprint; no headers/tokens. Fail if exact GET/PATCH tool absent. Run once on VPS to create fixture:

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_famachat_writer_schema.py \
  --output tests/fixtures/famachat-writer-tools.json
```

Review that fixture has no credential/PII before commit.

- [ ] **Step 2: Write FamaChat client tests from captured fixture**

The fake MCP transport asserts the **exact** argument envelope from fixture. Parse one client object and require fields id/phone/broker/status/source. Malformed/multiple objects -> controlled invalid response.

- [ ] **Step 3: Write dry-run identity/state tests**

Fake Brain claim includes:

```python
{
  "status": "ok",
  "mode": "shadow",
  "effect": {
    "effect_id": "fx_1",
    "lease_token": "lease_1",
    "client_id": 12800,
    "expected_phone_e164": "5534999772714",
    "expected_status": "Sem Atendimento",
    "target_status": "Não Respondeu",
    "cause": "first_t1_send_success"
  }
}
```

Test formatted phone equivalence and mismatch, broker !=35, source mismatch, already target, unexpected status, invalid transition, Brain/FamaChat unavailable. In all dry-run tests mutation call count = 0.

- [ ] **Step 4: Implement validation table/phone matcher/Brain client**

One frozen transition set. Phone matcher has dedicated unit cases for 55 prefix, punctuation, ninth-digit difference, and non-equivalent numbers. No PII in exception strings.

- [ ] **Step 5: Add CLI entrypoint, run, commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_famachat_writer_schema tests.test_famachat_client tests.test_lifecycle_writer -v
git add scripts/capture_famachat_writer_schema.py tests/fixtures/famachat-writer-tools.json tests/test_famachat_writer_schema.py src/brain/famachat_client.py src/brain/lifecycle_writer.py tests/test_famachat_client.py tests/test_lifecycle_writer.py pyproject.toml
git commit -m "feat: add dry-run lifecycle writer"
```

---

### Task 2: Inspect conditional-write capability without mutation

**Files:**
- Create: `scripts/probe_famachat_conditional_status.py`
- Create: `tests/test_famachat_conditional_probe.py`
- Modify: `docs/runbook.md`

**Interface:**

```bash
python scripts/probe_famachat_conditional_status.py inspect \
  --schema tests/fixtures/famachat-writer-tools.json \
  --output /var/lib/brain/runtime/famachat-conditional-write-inspection.json
```

- [ ] **Step 1: Write inspection fixtures/tests**

PATCH schema with only id+status -> `NO_ATOMIC_PRECONDITION`; explicit expected status/version/ETag-like contract -> `CANDIDATE`; absent tool -> `UNAVAILABLE`.

- [ ] **Step 2: Implement strict candidate recognition**

Never call FamaChat tools in inspect mode. Output exact strategy/field + schema fingerprint, no secrets. Description-only hints may identify candidate but do not count as PASS until Task 3 proves stale rejection.

- [ ] **Step 3: Run live inspection**

If `NO_ATOMIC_PRECONDITION`/`UNAVAILABLE`, stop the write-enablement branch; dry-run deployment remains valid/safe terminal state.

- [ ] **Step 4: Commit**

```bash
git add scripts/probe_famachat_conditional_status.py tests/test_famachat_conditional_probe.py docs/runbook.md
git commit -m "chore: inspect FamaChat conditional status capability"
```

---

### Task 3A: Prove stale expected-state rejection on a disposable FamaChat client

**Files:**
- Modify: `scripts/probe_famachat_conditional_status.py`
- Modify: `tests/test_famachat_conditional_probe.py`
- Runtime only: `/var/lib/brain/runtime/famachat-conditional-write-proof.json`

**Precondition:** Task 2 = `CANDIDATE`; otherwise skip and keep writes disabled.

- [ ] **Step 1: Add hard disposable-client guard**

Probe requires `--test-client-id` equal to env `FAMACHAT_CONDITIONAL_TEST_CLIENT_ID`; no name-based inference. It GETs first and requires brokerId=35 plus operator-required initial status `Sem Atendimento`. The operator explicitly designates/recreates this client for the probe; never use a real lead.

- [ ] **Step 2: Prove the server rejects a stale predicate**

Using the candidate atomic field/strategy from the schema:

1. GET disposable client and capture current version/status.
2. Conditional mutation `Sem Atendimento -> Não Respondeu` using expected current state.
3. Readback must prove `Não Respondeu`.
4. Attempt stale conditional mutation `Sem Atendimento -> Em Atendimento` using the **old** expected state/version.
5. Server must reject/no-op stale mutation.
6. Final GET must still be `Não Respondeu`.

Do not automatically restore the disposable client; automatic restoration would add another production mutation. Recreate/reset it manually before rerunning the probe.

- [ ] **Step 3: Write proof only on PASS**

Proof includes strategy, exact conditional field/request schema, tool names, schema fingerprint, timestamp. No test client ID/phone/name/token/raw response.

- [ ] **Step 4: Commit probe code only**

```bash
git add scripts/probe_famachat_conditional_status.py tests/test_famachat_conditional_probe.py
git commit -m "test: prove FamaChat atomic status precondition"
```

---

### Task 3B: Implement exactly the proven atomic strategy

**Files:**
- Create: `tests/fixtures/famachat-conditional-write-proof.json` (non-secret copy of PASS strategy metadata)
- Modify: `src/brain/famachat_client.py`
- Modify: `src/brain/lifecycle_writer.py`
- Modify: `tests/test_famachat_client.py`
- Modify: `tests/test_lifecycle_writer.py`

**Precondition:** Runtime proof PASS. Otherwise do not implement/enable mutation.

- [ ] **Step 1: Freeze one strategy fixture**

No generic “try fields” implementation. The test fixture names one strategy/field/schema fingerprint proven in Task 3A.

- [ ] **Step 2: Write exact mutation/stale-conflict tests**

Assert exact MCP call envelope from proof; stale predicate is conflict; network timeout is ambiguous and forces GET before any retry decision.

- [ ] **Step 3: Implement proof/fingerprint gate at startup**

If write enabled and runtime proof missing/stale/fingerprint mismatch/unsupported strategy -> startup refusal and health red. Dry-run may still start without proof.

- [ ] **Step 4: Implement mutate + mandatory readback**

```text
claim
GET exact client
prove id + phone + broker + source + expected status
conditional atomic PATCH expected->target
GET same ID
prove target -> applied
already target before patch -> already_applied
stale/human state -> conflict
ambiguous transport error -> GET before classifying/retrying
```

- [ ] **Step 5: Run/commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_famachat_client tests.test_lifecycle_writer -v
git add tests/fixtures/famachat-conditional-write-proof.json src/brain/famachat_client.py src/brain/lifecycle_writer.py tests/test_famachat_client.py tests/test_lifecycle_writer.py
git commit -m "feat: add proof-gated atomic lifecycle writes"
```

---

### Task 4: Add writer runtime/service and crash-after-write recovery

**Files:**
- Modify: `src/brain/lifecycle_writer.py`
- Create: `deploy/brain-lifecycle-writer.service`
- Create: `deploy/brain-lifecycle-writer.env.example`
- Modify: `deploy/brain.env.example`
- Modify: `tests/test_deployment_contracts.py`
- Modify: `docs/runbook.md`

- [ ] **Step 1: Write crash recovery test**

Mutation succeeds, process dies before result POST, lease expires, next writer reclaims, GET sees target, reports `already_applied`, no second PATCH.

- [ ] **Step 2: Implement bounded loop/health**

Idle poll ~2s; failures exponential backoff <=60s. Local health `127.0.0.1:8776` exposes mode/reachability/last result only, no PII. Lost lease requires re-claim+GET before any mutation.

- [ ] **Step 3: Create hardened systemd unit**

`UMask=0077`, `EnvironmentFile=/etc/brain/writer.env`, `Restart=on-failure`, `NoNewPrivileges=true`; no Hermes-session or upstream writable path.

- [ ] **Step 4: Run/commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_writer tests.test_deployment_contracts -v
git add src/brain/lifecycle_writer.py deploy/brain-lifecycle-writer.service deploy/brain-lifecycle-writer.env.example deploy/brain.env.example tests/test_deployment_contracts.py docs/runbook.md
git commit -m "chore: deploy lifecycle writer safely"
```

---

### Task 5: Enforce upstream Hermes integrity/compatibility gates

**Files:**
- Create: `scripts/hermes_integrity.py`
- Create: `tests/test_hermes_integrity.py`
- Modify: `scripts/hermes_integration_check.py`
- Modify: `docs/runbook.md`

**Interfaces:**
- Capture/verify Git HEAD/status + SHA256 of critical upstream files, strictly read-only.

- [ ] **Step 1: Write fake-repo tests**

Clean capture/verify pass; changed protected file/dirty worktree fails. Ensure checker never runs checkout/reset/stash/add/commit.

- [ ] **Step 2: Implement critical manifest**

At minimum: `scripts/whatsapp-bridge/bridge.js`, `plugins/platforms/whatsapp/adapter.py`, `gateway/delivery_ledger.py`, `gateway/session.py`, `gateway/session_context.py`, `tools/kanban_tools.py`, plus HEAD/status.

- [ ] **Step 3: Extend compatibility checker**

Require public hooks/turn_id/modify directive, session ContextVars, delivery-ledger schema/semantics, batching assumptions. Compatibility failure disables Brain lifecycle but never “repairs” Hermes.

- [ ] **Step 4: Document/run/commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_hermes_integrity -v
git add scripts/hermes_integrity.py scripts/hermes_integration_check.py tests/test_hermes_integrity.py docs/runbook.md
git commit -m "test: enforce upstream Hermes integrity"
```

---

### Task 6: Add shadow/write rollout gate and execute phased rollout

**Files:**
- Create: `scripts/ctwa_rollout_gate.py`
- Create: `tests/test_ctwa_rollout_gate.py`
- Modify: `docs/runbook.md`
- Modify: `README.md`

- [ ] **Step 1: Write exact gate tests**

Shadow requires observer coexistence/raw CTWA/context/correlation/idempotency/Cadastro readback/Reno history/lifecycle shadow/restart recovery/Hermes compatibility/integrity. Write mode additionally requires conditional-write proof+fingerprint and successful writer dry-run. Missing/FAIL/NOT_PROVEN -> nonzero.

- [ ] **Step 2: Implement gate as observer only**

Gate prints PASS/FAIL; it never edits env or starts/stops services and never enables write itself.

- [ ] **Step 3: Phase A/B production shadow**

Deploy Brain context + observer + operational Profile contracts + lifecycle engine with writes false. Controlled E2E evidence file `/var/lib/brain/runtime/ctwa-shadow-results.json` contains only gate names/status/technical evidence IDs/timestamps, no phone/text/client PII.

Scenarios: CTWA-only; T1 success; human after T1; human before T1; CTWA+human in debounce; second CTWA-attributed not human; `JA_E_CLIENTE`; `CORRETOR_ATIVO`; Cadastro readback fail; T1 fail; Brain restart; observer replay; effect supersession; manual CRM conflict.

- [ ] **Step 4: Phase D writer dry-run**

Start writer with write disabled; require real claim/GET/id+phone+broker+source/status validation and `would_apply`, zero PATCH.

- [ ] **Step 5: Run write gate and explicit operator enable only on PASS**

No backfill. A failed/unproven conditional gate leaves dry-run as correct terminal production state.

- [ ] **Step 6: Full quality + integrity verification and commit**

```bash
uv run ruff check src tests scripts integrations
uv run ruff format --check src tests scripts integrations
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
cd observers/whatsapp && npm test
```

Re-run upstream integrity verify; expected unchanged.

```bash
git add scripts/ctwa_rollout_gate.py tests/test_ctwa_rollout_gate.py docs/runbook.md README.md
git commit -m "chore: add CTWA shadow and go-live gates"
```

## Plan 5 Acceptance Gate

Dry-run safe terminal state:

```text
WRITER_NO_LLM=PASS
WRITER_CONTACT_PHONE_MATCH=PASS
WRITER_DRY_RUN=PASS
WRITER_CONFLICT_FAIL_CLOSED=PASS
HERMES_COMPATIBILITY=PASS
HERMES_ORIGINAL_INTEGRITY=PASS
FAMACHAT_WRITES=ZERO
```

Write mode additionally requires:

```text
FAMACHAT_CONDITIONAL_WRITE=PASS
CONDITIONAL_SCHEMA_FINGERPRINT_MATCH=PASS
CRASH_AFTER_WRITE_RECOVERY=PASS
LIFECYCLE_SHADOW=PASS
RESTART_RECOVERY=PASS
WRITE_GATE=PASS
BRAIN_LIFECYCLE_WRITE_ENABLED=true  # explicit operator action after gate
```

If atomic conditional mutation is not proven, **stop at shadow/dry-run by design**; never substitute ordinary GET→PATCH.
