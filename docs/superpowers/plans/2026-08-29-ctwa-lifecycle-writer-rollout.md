# CTWA Lifecycle Writer & Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the non-LLM lifecycle writer, prove whether FamaChat supports an atomic expected-current-status mutation, keep all writes disabled until that proof passes, and provide integrity/shadow/go-live gates that allow rollout without editing or restarting upstream Hermes code.

**Architecture:** `brain-lifecycle-writer.service` polls Brain's authenticated lifecycle claim API, reads the exact FamaChat client through the FamaChat MCP server as a deterministic MCP client, validates client/broker/source/current status, and defaults to dry-run. A separate capability probe inspects and safely tests the live `fc_patch_clientes_by_id` schema on an operator-designated test client. Production write mode is impossible unless a machine-readable proof artifact records a supported atomic expected-state mechanism. The writer then uses only that proven mechanism, performs readback, and reports `applied`/`already_applied`/`conflict`/`retryable`/`permanent_failure` to Brain. Upstream Hermes integrity is captured before deployment and verified unchanged afterward.

**Tech Stack:** Python 3.11+, MCP 2.0 deterministic client, standard-library HTTP/JSON/hashlib, systemd, `unittest`, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Writer contains no LLM/model/provider code and never calls Hermes Agent tools through a model.
- Writer does not modify Hermes `state.db`, `kanban.db`, source files, config, or WhatsApp session.
- Writer credential is distinct from CEO/worker/observer Brain credentials.
- FamaChat write credential exists only in writer environment (`/etc/brain/writer.env`, `0600`); it is never exposed to Brain MCP tools, Profiles, Kanban cards, logs, or Git.
- Default `BRAIN_LIFECYCLE_WRITE_ENABLED=false`.
- Write mode cannot be enabled unless `/var/lib/brain/runtime/famachat-conditional-write-proof.json` exists, is schema-valid, names a supported writer strategy, records `PASS`, and matches the current FamaChat tool schema fingerprint.
- Allowed lifecycle transitions only:
  - `Sem Atendimento -> Não Respondeu`
  - `Sem Atendimento -> Em Atendimento`
  - `Não Respondeu -> Em Atendimento`
- Writer never changes `brokerId`, phone, name, source, notes, appointments, or any status outside those three transitions.
- Writer validates exact `client_id`, `brokerId=35`, `source=Facebook Ads`, and live status before a write. If current status is already target -> `already_applied`; if outside expected/target -> `conflict`, no write.
- GET→PATCH→GET without a proven server-side expected-state precondition is not an acceptable production mutation. If no atomic mechanism is proven, dry-run remains the terminal production state.
- No retroactive/backfill lifecycle effects. Only Brain lifecycles created after feature rollout are eligible.
- Every task follows TDD and ends in a focused test plus commit.

---

### Task 1: Implement a deterministic FamaChat MCP client and read-only writer loop

**Files:**
- Create: `src/brain/famachat_client.py`
- Create: `src/brain/lifecycle_writer.py`
- Create: `tests/test_famachat_client.py`
- Create: `tests/test_lifecycle_writer.py`
- Modify: `pyproject.toml`

**Interfaces:**
- `FamaChatClient` connects to configured Streamable HTTP MCP server and calls only explicit tool names supplied by writer code.
- `get_client(client_id) -> FamaChatClientRecord` calls `fc_get_clientes_by_id`.
- `LifecycleWriter.run_once() -> WriterIterationResult` claims one Brain effect and validates it without mutating when write mode is false.
- CLI entrypoint: `brain-lifecycle-writer = brain.lifecycle_writer:main`.

- [ ] **Step 1: Write FamaChat client contract tests with a fake MCP transport**

Prove `get_client(12800)` makes exactly one call:

```python
("fc_get_clientes_by_id", {"path": {"id": 12800}})
```

If live MCP schema in the environment uses a different exact argument envelope, record the observed `tools/list` schema in the test fixture and use that exact envelope before implementing. Do not guess at runtime.

Parse one exact client object into:

```python
@dataclass(frozen=True)
class FamaChatClientRecord:
    client_id: int
    broker_id: int | None
    status: str | None
    source: str | None
```

Malformed/multiple objects -> controlled invalid-response error.

- [ ] **Step 2: Write dry-run writer tests**

Fake Brain claim:

```python
{
  "status": "ok",
  "mode": "shadow",
  "effect": {
    "effect_id": "fx_1",
    "lease_token": "lease_1",
    "client_id": 12800,
    "expected_status": "Sem Atendimento",
    "target_status": "Não Respondeu",
    "cause": "first_t1_send_success"
  }
}
```

Fake FamaChat returns exact valid client. Assert zero mutation calls and Brain receives a `would_apply`/shadow result rather than `applied`.

Also test broker !=35, source !=`Facebook Ads`, already target, unexpected status, invalid transition, Brain unavailable, and FamaChat unavailable.

- [ ] **Step 3: Implement Brain writer client and validation table**

Keep allowed transitions as one frozen set:

```python
ALLOWED_TRANSITIONS = frozenset({
    ("Sem Atendimento", "Não Respondeu"),
    ("Sem Atendimento", "Em Atendimento"),
    ("Não Respondeu", "Em Atendimento"),
})
```

The writer reports conflict rather than mutating when any live state is not exact.

- [ ] **Step 4: Add CLI entrypoint and run tests**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_famachat_client tests.test_lifecycle_writer -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain/famachat_client.py src/brain/lifecycle_writer.py tests/test_famachat_client.py tests/test_lifecycle_writer.py pyproject.toml
git commit -m "feat: add dry-run lifecycle writer"
```

---

### Task 2: Build a read-only FamaChat conditional-write capability inspector

**Files:**
- Create: `scripts/probe_famachat_conditional_status.py`
- Create: `tests/test_famachat_conditional_probe.py`
- Modify: `docs/runbook.md`

**Interfaces:**
- Command:

```bash
python scripts/probe_famachat_conditional_status.py inspect \
  --output /var/lib/brain/runtime/famachat-conditional-write-inspection.json
```

- Inspection calls FamaChat MCP initialize/tools-list only; it performs no mutation.
- It fingerprints the `fc_get_clientes_by_id` and `fc_patch_clientes_by_id` input schemas and detects only explicit conditional candidates: expected-status field, version/revision field, ETag/If-Match capability represented in the tool contract, or another documented atomic precondition encoded in schema/description.

- [ ] **Step 1: Write schema-inspection tests**

Fixture A: patch tool only has `{path.id, body.status}` -> result `NO_ATOMIC_PRECONDITION`.
Fixture B: patch tool has an explicit `expectedStatus`/equivalent precondition -> result `CANDIDATE`, recording the exact field path.
Fixture C: tool absent -> `UNAVAILABLE`.

- [ ] **Step 2: Implement inspection without invoking tools**

Output contains only non-secret schema metadata:

```json
{
  "schema_version": 1,
  "status": "CANDIDATE",
  "read_tool": "fc_get_clientes_by_id",
  "write_tool": "fc_patch_clientes_by_id",
  "schema_fingerprint": "sha256:...",
  "candidate_strategy": "expected_status_field",
  "candidate_field": "body.expectedStatus"
}
```

Never include auth headers or environment values.

- [ ] **Step 3: Run tests and live read-only inspection**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_famachat_conditional_probe -v
python scripts/probe_famachat_conditional_status.py inspect \
  --output /var/lib/brain/runtime/famachat-conditional-write-inspection.json
```

If status is `NO_ATOMIC_PRECONDITION` or `UNAVAILABLE`, record the result in the runbook and **stop the write-enablement branch here**. Tasks 3B/4 write-mode tests remain blocked; the dry-run writer is still deployable.

- [ ] **Step 4: Commit**

```bash
git add scripts/probe_famachat_conditional_status.py tests/test_famachat_conditional_probe.py docs/runbook.md
git commit -m "chore: inspect FamaChat conditional status capability"
```

---

### Task 3A: Verify a candidate conditional mechanism safely on a dedicated test client

**Files:**
- Modify: `scripts/probe_famachat_conditional_status.py`
- Modify: `tests/test_famachat_conditional_probe.py`
- Create at runtime only: `/var/lib/brain/runtime/famachat-conditional-write-proof.json`

**Precondition:** Task 2 inspection returned `CANDIDATE`. Otherwise this task is skipped and production write mode remains permanently disabled until FamaChat gains a supported atomic mechanism.

**Interfaces:**
- Command requires operator-supplied `--test-client-id` and `--expected-current-status`; it refuses client `brokerId !=35` and refuses any non-test source/tag required by the local test policy.
- The test uses an operator-designated disposable/synthetic FamaChat client and never a live lead.

- [ ] **Step 1: Add safe-probe guards in tests**

Prove the command refuses to run without explicit `--test-client-id`, refuses a client that is not marked as the operator's dedicated test fixture, and never tries a status outside the approved lifecycle set.

Define the dedicated test-client eligibility check from a stable operator-controlled marker. Prefer a FamaChat test/sandbox flag if present in the read schema. If no such flag exists, require an exact test client ID allowlist in `/etc/brain/writer.env` (`FAMACHAT_CONDITIONAL_TEST_CLIENT_ID`) and equality with CLI input; do not infer test status from the client's name.

- [ ] **Step 2: Implement a two-request stale-writer proof**

The proof must demonstrate that the server rejects a stale expected state, not merely that a conditional field exists:

1. GET test client and record current status/version.
2. Perform one conditional no-op or reversible test mutation using the candidate precondition.
3. Attempt a second mutation using the now-stale precondition.
4. Require the second call to be rejected/no-op by the server.
5. GET readback and restore the dedicated test fixture if the first probe changed it, using the same proven atomic mechanism.

If the candidate cannot be tested without risking persistent state or cannot restore safely, mark `FAIL_UNSAFE_TO_PROVE` and keep writes disabled.

- [ ] **Step 3: Write the proof artifact only on PASS**

Example:

```json
{
  "schema_version": 1,
  "status": "PASS",
  "strategy": "expected_status_field",
  "field": "body.expectedStatus",
  "read_tool": "fc_get_clientes_by_id",
  "write_tool": "fc_patch_clientes_by_id",
  "schema_fingerprint": "sha256:...",
  "proved_at": "2026-08-29T...Z"
}
```

No client ID, phone, name, token, body content, or raw API response goes in the artifact.

- [ ] **Step 4: Commit probe implementation (not runtime proof artifact)**

```bash
git add scripts/probe_famachat_conditional_status.py tests/test_famachat_conditional_probe.py
git commit -m "test: prove FamaChat atomic status precondition"
```

---

### Task 3B: Implement the one proven conditional status strategy

**Files:**
- Modify: `src/brain/famachat_client.py`
- Modify: `src/brain/lifecycle_writer.py`
- Modify: `tests/test_famachat_client.py`
- Modify: `tests/test_lifecycle_writer.py`

**Precondition:** Runtime proof artifact from Task 3A is `PASS`. If not, do not implement/enable mutation; retain Task 1 dry-run writer.

- [ ] **Step 1: Turn the proof into an exact fixture**

Copy only the non-secret proof shape/schema fingerprint/strategy into `tests/fixtures/famachat-conditional-write-proof.json` (create this file). The fixture must identify one implemented strategy; no generic “try fields until one works” code is allowed.

- [ ] **Step 2: Write mutation tests from that fixture**

Test exact MCP tool name and exact argument shape. Require expected current status/version in the request. Test stale-precondition conflict separately from transport errors.

- [ ] **Step 3: Implement proof-gated strategy selection**

At writer startup:

```python
proof = load_conditional_write_proof(path)
if write_enabled and not proof.matches(current_schema_fingerprint):
    raise RuntimeError("conditional write proof missing or stale")
```

Only the strategy named by the proof is instantiable. Unsupported strategy -> startup refusal.

- [ ] **Step 4: Implement mutation + mandatory readback**

Writer flow in write mode:

```text
claim effect
GET client
validate exact id/broker/source/current status
atomic conditional PATCH(expected current -> target)
GET same client by id
if target proven: applied
if server says stale or readback shows a different human state: conflict
if already target before mutation: already_applied
```

Never retry a mutation blindly after an ambiguous transport timeout; GET first, then classify `already_applied` vs conflict vs retryable.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_famachat_client tests.test_lifecycle_writer -v
git add src/brain/famachat_client.py src/brain/lifecycle_writer.py tests/test_famachat_client.py tests/test_lifecycle_writer.py tests/fixtures/famachat-conditional-write-proof.json
git commit -m "feat: add proof-gated atomic lifecycle writes"
```

---

### Task 4: Add writer systemd service, health, and crash-recovery behavior

**Files:**
- Modify: `src/brain/lifecycle_writer.py`
- Create: `deploy/brain-lifecycle-writer.service`
- Create: `deploy/brain-lifecycle-writer.env.example`
- Modify: `deploy/brain.env.example`
- Modify: `tests/test_deployment_contracts.py`
- Modify: `docs/runbook.md`

**Interfaces:**
- Local health: `GET http://127.0.0.1:8776/health` -> mode, Brain reachability, FamaChat reachability, last iteration/result; no PII.
- systemd defaults `BRAIN_LIFECYCLE_WRITE_ENABLED=false`.

- [ ] **Step 1: Write crash-after-write recovery test**

Simulate:

```text
writer atomic mutation succeeds
process dies before lifecycle/result POST
lease expires
next writer claims same effect
GET client returns target status
writer reports already_applied
no second PATCH
```

- [ ] **Step 2: Implement loop and bounded backoff**

Poll Brain approximately every 2 seconds when idle, with exponential backoff on Brain/FamaChat failures capped at 60 seconds. One effect per claim. A failed effect result POST is retried with the same lease token while valid; after lease loss, do not mutate again without re-claiming and re-reading FamaChat.

- [ ] **Step 3: Create hardened unit/env example**

Unit requirements: `UMask=0077`, `EnvironmentFile=/etc/brain/writer.env`, `Restart=on-failure`, `NoNewPrivileges=true`, no access need to Hermes WhatsApp session, no writable upstream paths.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle_writer tests.test_deployment_contracts -v
git add src/brain/lifecycle_writer.py deploy/brain-lifecycle-writer.service deploy/brain-lifecycle-writer.env.example deploy/brain.env.example tests/test_deployment_contracts.py docs/runbook.md
git commit -m "chore: deploy lifecycle writer safely"
```

---

### Task 5: Add upstream Hermes integrity capture/verify and compatibility rollout gates

**Files:**
- Create: `scripts/hermes_integrity.py`
- Create: `tests/test_hermes_integrity.py`
- Modify: `scripts/hermes_integration_check.py`
- Modify: `docs/runbook.md`

**Interfaces:**
- Capture:

```bash
python scripts/hermes_integrity.py capture \
  --root /usr/local/lib/hermes-agent \
  --output /var/lib/brain/runtime/hermes-integrity-baseline.json
```

- Verify:

```bash
python scripts/hermes_integrity.py verify \
  --root /usr/local/lib/hermes-agent \
  --baseline /var/lib/brain/runtime/hermes-integrity-baseline.json
```

- Critical files: `scripts/whatsapp-bridge/bridge.js`, `plugins/platforms/whatsapp/adapter.py`, `gateway/delivery_ledger.py`, `gateway/session.py`, `gateway/session_context.py`, `tools/kanban_tools.py` plus repository HEAD/status.

- [ ] **Step 1: Write fixture-repository tests**

Use a temporary fake git repo; capture clean baseline, mutate one protected file, assert verify exits nonzero and names only the relative path/hash mismatch. Add dirty-worktree test.

- [ ] **Step 2: Implement strictly read-only checker**

The script may run `git rev-parse HEAD`, `git status --porcelain`, and SHA256 reads. It must never run checkout/reset/stash/add/commit or write inside root.

- [ ] **Step 3: Extend compatibility checker**

Keep/extend Plan 1 checks for public hooks, `turn_id`, modify directives, session ContextVars, delivery ledger schema, and WhatsApp batching semantics. Report `HERMES_COMPATIBILITY=PASS|FAIL` separately from byte-integrity status.

- [ ] **Step 4: Document deployment order**

Runbook requires baseline capture before Brain/Profile rollout, verify after every rollout stage, and an immediate stop if `HERMES_ORIGINAL_INTEGRITY=FAIL`.

- [ ] **Step 5: Run and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_hermes_integrity -v
git add scripts/hermes_integrity.py scripts/hermes_integration_check.py tests/test_hermes_integrity.py docs/runbook.md
git commit -m "test: enforce upstream Hermes integrity"
```

---

### Task 6: Build the shadow/go-live gate and perform phased rollout

**Files:**
- Create: `scripts/ctwa_rollout_gate.py`
- Create: `tests/test_ctwa_rollout_gate.py`
- Modify: `docs/runbook.md`
- Modify: `README.md`

**Interfaces:**
- `ctwa_rollout_gate.py --mode shadow|write` calls Brain/observer/writer health and consumes a non-PII scenario-result JSON produced by controlled E2E runs.
- `--mode write` additionally requires conditional-write proof PASS + fingerprint match + upstream integrity PASS.

- [ ] **Step 1: Write exact gate tests**

Shadow requirements:

```text
OBSERVER_COEXISTENCE
RAW_CTWA_CAPTURE
CONVERSATION_CONTEXT_E2E
TURN_CORRELATION_CASES
KANBAN_IDEMPOTENCY
CADASTRO_READBACK
RENO_FIRST_HISTORY
LIFECYCLE_SHADOW
RESTART_RECOVERY
HERMES_COMPATIBILITY
HERMES_ORIGINAL_INTEGRITY
```

Write mode adds `FAMACHAT_CONDITIONAL_WRITE` and writer dry-run validation. Any `FAIL`, `NOT_PROVEN`, missing key, stale proof, or unhealthy component exits nonzero.

- [ ] **Step 2: Implement gate with no automatic write enabling**

The script prints `WRITE_GATE=PASS|FAIL` but never edits env files or starts/stops services. The operator must explicitly change `BRAIN_LIFECYCLE_WRITE_ENABLED` after a PASS.

- [ ] **Step 3: Execute Phase A/B shadow rollout**

Deploy Brain context + observer + Profile contracts + lifecycle engine with writer absent or write disabled. Run controlled real CTWA scenarios and record only PASS/FAIL/NOT_PROVEN evidence IDs/timestamps in `/var/lib/brain/runtime/ctwa-shadow-results.json`; no phone/text/client PII.

Required scenarios: CTWA-only; T1 success; human after T1; human before T1; CTWA+human inside debounce; `JA_E_CLIENTE`; `CORRETOR_ATIVO`; Cadastro readback fail; Hermes T1 fail; Brain restart; observer replay/dedup; effect supersession.

- [ ] **Step 4: Execute Phase D writer dry-run**

Start writer with `BRAIN_LIFECYCLE_WRITE_ENABLED=false`; require it to claim/validate eligible effects and report `would_apply` without PATCH. Add results to the shadow evidence file.

- [ ] **Step 5: Run write gate**

```bash
python scripts/ctwa_rollout_gate.py --mode write \
  --results /var/lib/brain/runtime/ctwa-shadow-results.json \
  --conditional-proof /var/lib/brain/runtime/famachat-conditional-write-proof.json \
  --integrity-baseline /var/lib/brain/runtime/hermes-integrity-baseline.json
```

If not PASS, leave write disabled. If PASS, operator may explicitly enable the writer for **new lifecycles only**; do not backfill old CRM records.

- [ ] **Step 6: Full quality gate and final commit**

```bash
uv run ruff check src tests scripts integrations
uv run ruff format --check src tests scripts integrations
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
cd observers/whatsapp && npm test
```

Then re-run upstream integrity verify. Expected: PASS, upstream unchanged.

```bash
git add scripts/ctwa_rollout_gate.py tests/test_ctwa_rollout_gate.py docs/runbook.md README.md
git commit -m "chore: add CTWA shadow and go-live gates"
```

## Plan 5 Acceptance Gate

Dry-run deployment is acceptable with:

```text
WRITER_NO_LLM=PASS
WRITER_DRY_RUN=PASS
WRITER_CONFLICT_FAIL_CLOSED=PASS
CRASH_AFTER_WRITE_RECOVERY=PASS (unit test; write branch only when atomic proof exists)
HERMES_COMPATIBILITY=PASS
HERMES_ORIGINAL_INTEGRITY=PASS
FAMACHAT_WRITES=ZERO
```

Production lifecycle writes require all of the above plus:

```text
FAMACHAT_CONDITIONAL_WRITE=PASS
CONDITIONAL_SCHEMA_FINGERPRINT_MATCH=PASS
LIFECYCLE_SHADOW=PASS
RESTART_RECOVERY=PASS
WRITE_GATE=PASS
BRAIN_LIFECYCLE_WRITE_ENABLED=true  # explicit operator action only after gate
```

If FamaChat conditional write remains unproven, **the implementation stops at shadow/dry-run by design**; this is a correct safe terminal state, not a reason to substitute non-atomic GET→PATCH.
