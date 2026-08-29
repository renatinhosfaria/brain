# CTWA Brain Lifecycle Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. The five component plans below are the executable source of truth for individual code/config steps.

**Goal:** Execute the approved CTWA/Brain architecture in dependency order while preserving upstream Hermes Agent byte-for-byte and keeping FamaChat lifecycle writes disabled until all shadow, compatibility, integrity, and atomic-write gates pass.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Component Plans

1. `docs/superpowers/plans/2026-08-29-ctwa-brain-transport-context.md`
   - Brain runtime DB, privacy-safe event ingestion, turn correlation, `conversation_context()`, CEO plugin hooks/idempotency.
2. `docs/superpowers/plans/2026-08-29-ctwa-whatsapp-observer.md`
   - second linked-device observer, own pinned Baileys, safe durable outbox, health/systemd.
3. `docs/superpowers/plans/2026-08-29-ctwa-hermes-profile-contracts.md`
   - Fama-owned CEO/Porteiro/Cadastro/Reno SOUL/skills/config allowlists and deterministic workflow contracts.
4. `docs/superpowers/plans/2026-08-29-ctwa-lifecycle-engine.md`
   - Kanban/delivery reconciliation, exact lifecycle binding, facts/state/effects, writer claim/result APIs.
5. `docs/superpowers/plans/2026-08-29-ctwa-lifecycle-writer-rollout.md`
   - deterministic writer, FamaChat atomic-write proof, upstream integrity, shadow/go-live gates.

## Cross-plan invariants

- `/usr/local/lib/hermes-agent/**` is immutable. No patch, monkeypatch, preload, wrapper substitution, or write to upstream source/config/session.
- Fama-owned operational files in `renatinhosfaria/hermes` may change only as enumerated in Plan 3 above (component plan filename #3).
- Hermes state/Kanban DB access from Brain remains read-only.
- The observer safe envelope may include a `transport_kind` hint for diagnostics, but **Brain is authoritative**: Brain recomputes/validates `ctwa_candidate` vs ordinary from the sanitized CTWA fields before persistence/lifecycle use. A mismatch is rejected or normalized deterministically; never trust a free-form classification string from the wire.
- Observer outbox contains no raw message text, raw JID/LID, raw observer message ID, raw source ID, raw ctwaClid, or full URL. Safe HMAC/length/hostname metadata only, plus sanitized display name until its 24h expiry.
- `wa_turn_id` and `event_id` remain distinct. Kanban idempotency always uses `wa_turn_id`; lifecycle origin uses `event_id`.
- CEO uses one `conversation_context()` capability; worker `conversation_phone()` remains fallback only for Porteiro/Cadastro.
- Reno first new-lead history lookup is exactly one `conversation_recent()` call in that turn; an error is recorded, not retried in the same turn.
- Lifecycle transitions are deterministic code, never LLM-owned.
- Writer claim contact proof is transient: Brain resolves `expected_phone_e164` from lifecycle contact_key/current trusted mappings, writer compares it to FamaChat GET, neither service persists/logs the raw phone.
- FamaChat is current-state authority. No lifecycle write without exact id/contact/broker/source/status proof.
- Ordinary GET→PATCH is never promoted to production write. Missing atomic expected-state proof means dry-run indefinitely.
- No backfill.

## Execution Order

### Stage 0 — Clean baselines

- [ ] Record current Brain and operational Hermes repo HEAD/status.
- [ ] Record installed upstream Hermes version/HEAD/status and critical-file hashes using the already-approved manual preflight method until the automated integrity script exists.
- [ ] Confirm current Hermes WhatsApp health/queue and Brain health.
- [ ] Do not pair/restart/change any upstream Hermes component as part of implementation setup.

### Stage 1 — Brain transport/context foundation

- [ ] Execute all tasks in `2026-08-29-ctwa-brain-transport-context.md` using TDD.
- [ ] Run Plan 1 acceptance gate locally/against read-only supported Hermes evidence.
- [ ] Do not deploy a status writer.

**Gate:** Plan 1 all PASS, upstream Hermes untouched.

### Stage 2 — Implement integrity checker before first production deployment

The integrity task is physically documented as Task 5 of the writer/rollout plan, but it is a cross-cutting deployment prerequisite and must be implemented now.

- [ ] Execute **Task 5 only** from `2026-08-29-ctwa-lifecycle-writer-rollout.md`.
- [ ] Capture `/var/lib/brain/runtime/hermes-integrity-baseline.json` before installing/restarting any Brain observer/plugin change on the VPS.
- [ ] Verify baseline immediately; must PASS.

**Gate:** `HERMES_ORIGINAL_INTEGRITY=PASS`.

### Stage 3 — Production observer in capture-only mode

- [ ] Execute `2026-08-29-ctwa-whatsapp-observer.md` completely.
- [ ] Install `brain-whatsapp-observer.service` with its own session and secrets.
- [ ] Pair it manually as a second linked device; never disconnect Hermes device.
- [ ] Repeat one controlled CTWA and require Brain safe ingest + `conversation_context()` correlation.
- [ ] Re-run upstream integrity verify.

**Gate:** Plan 2 acceptance gate + real `CONVERSATION_CONTEXT_E2E=PASS`; no CRM write capability enabled.

### Stage 4 — Operational Profile contracts and least privilege

- [ ] Execute `2026-08-29-ctwa-hermes-profile-contracts.md` in the operational Hermes repo.
- [ ] Generate Reno exact FamaChat read allowlist from live MCP `tools/list`; no wildcard.
- [ ] Run `verify_team.py core` then `full`.
- [ ] Re-run upstream integrity verify after any gateway/Profile restart required to load Fama-owned config/plugin changes.

**Gate:** Plan 4 acceptance gate, upstream untouched.

### Stage 5 — Lifecycle shadow engine

- [ ] Execute `2026-08-29-ctwa-lifecycle-engine.md` completely.
- [ ] Run controlled shadow scenarios using real Kanban/delivery evidence.
- [ ] Keep FamaChat writes zero.
- [ ] Re-run upstream integrity verify.

**Gate:** Plan 3 acceptance gate including exact lifecycle/contact binding, delivery proof, effect supersession, restart reconciliation.

### Stage 6 — Writer dry-run and atomic FamaChat proof

- [ ] Execute Plan 5 Task 1 (schema capture + dry-run writer).
- [ ] Execute Plan 5 Task 2 (read-only conditional capability inspection).
- [ ] If inspection has no candidate atomic precondition, skip Tasks 3A/3B and deploy dry-run only.
- [ ] If candidate exists, execute Task 3A on the operator-designated disposable test client only.
- [ ] Only if Task 3A writes PASS proof, execute Task 3B to implement exactly that proven strategy.
- [ ] Execute Plan 5 Task 4 writer systemd/crash-recovery work.
- [ ] Task 5 integrity checker is already implemented from Stage 2; rerun it, do not duplicate implementation.
- [ ] Execute Plan 5 Task 6 shadow/write rollout gate.

**Gate:** write mode stays false unless every write gate is PASS.

### Stage 7 — Explicit production write activation

This is an operator action, not an automatic script action.

- [ ] Require `ctwa_rollout_gate.py --mode write` exit 0.
- [ ] Require current conditional-write proof fingerprint equal current live tool schema fingerprint.
- [ ] Require upstream Hermes integrity PASS immediately before activation.
- [ ] Explicitly set `BRAIN_LIFECYCLE_WRITE_ENABLED=true` only after all above.
- [ ] Start/restart writer service only; no Hermes Agent code/config/session action.
- [ ] Observe first new CTWA lifecycle end-to-end; no historical backfill.
- [ ] Re-run upstream integrity verify after activation.

## Required controlled E2E matrix

Before write mode:

```text
CTWA_ONLY_T0=PASS
T1_SEND_SUCCESS_DESIRES_NAO_RESPONDEU=PASS
HUMAN_AFTER_T1_DESIRES_EM_ATENDIMENTO=PASS
HUMAN_BEFORE_T1_SKIPS_NAO_RESPONDEU=PASS
CTWA_PLUS_HUMAN_SAME_DEBOUNCE=PASS
SECOND_CTWA_ATTRIBUTED_NOT_HUMAN=PASS
JA_E_CLIENTE_NO_LIFECYCLE=PASS
CORRETOR_ATIVO_NO_LIFECYCLE=PASS
CADASTRO_READBACK_FAIL_NO_RENO_LIFECYCLE=PASS
HERMES_T1_FAIL_NO_DELIVERY_FACT=PASS
BRAIN_RESTART_RECOVERY=PASS
OBSERVER_REPLAY_DEDUP=PASS
EFFECT_SUPERSESSION=PASS
MANUAL_CRM_STATE_NEVER_DOWNGRADED=PASS
```

## Final Go-live Gate

Every value below must be PASS:

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
FAMACHAT_CONDITIONAL_WRITE
CONDITIONAL_SCHEMA_FINGERPRINT_MATCH
HERMES_COMPATIBILITY
HERMES_ORIGINAL_INTEGRITY
WRITE_GATE
```

Any FAIL/NOT_PROVEN/missing result keeps `BRAIN_LIFECYCLE_WRITE_ENABLED=false`.

## Completion Definition

Implementation is complete only when:

- all five component plans' applicable tasks are checked and committed;
- all unit/integration/operational verification suites pass;
- observer and writer secrets/session paths have correct permissions;
- upstream Hermes integrity is unchanged from pre-deployment baseline;
- production write mode is either safely enabled after all gates, **or intentionally remains dry-run because FamaChat atomic conditional write is not proven**.

The second outcome is a valid safe completion state; never weaken the atomic-write requirement just to mark the project “done.”
