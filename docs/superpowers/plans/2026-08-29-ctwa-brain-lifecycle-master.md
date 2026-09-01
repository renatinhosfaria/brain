# CTWA Brain Capture Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. The three component plans below are the executable source of truth for individual code/config steps.

**Goal:** Capture Meta Ads Click-to-WhatsApp attribution that Hermes does not expose, serve it to the CEO as trusted turn-free context, and keep the upstream Hermes Agent byte-for-byte unchanged.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

**Amended 2026-08-31 (Amendment 2).** Automated CRM lifecycle writing is out of scope. Reno owns the status transitions through its own MCP surface. The lifecycle shadow engine and the write-activation stages are gone, along with two component plans; the stages below were renumbered so there is one unambiguous sequence. What remains is capture, context, least privilege and integrity. Section 2.5 and Amendment 2 of the spec record why.

## Component Plans

1. `docs/superpowers/plans/2026-08-29-ctwa-brain-transport-context.md`
   - Brain runtime DB, privacy-safe transport facts, retention enforcement, and the contact-scoped `conversation_context()` the CEO plugin serves.
2. `docs/superpowers/plans/2026-08-29-ctwa-whatsapp-observer.md`
   - second linked-device observer, own pinned Baileys, safe durable outbox, health/systemd.
3. `docs/superpowers/plans/2026-08-29-ctwa-hermes-profile-contracts.md`
   - Fama-owned CEO/Porteiro/Cadastro/Reno SOUL/skills/config allowlists and deterministic workflow contracts, including Reno's status-write contract.

Removed by Amendment 2: the lifecycle engine plan and the writer/rollout plan. Their one surviving requirement, the upstream integrity gate, lives in `docs/runbook.md`.

## Cross-plan invariants

- `/usr/local/lib/hermes-agent/**` is immutable. No patch, monkeypatch, preload, wrapper substitution, or write to upstream source/config/session.
- Fama-owned operational files in `renatinhosfaria/hermes` may change only as enumerated in component plan 3.
- Hermes state/Kanban DB access from Brain remains read-only.
- The observer safe envelope may include a `transport_kind` hint for diagnostics, but **Brain is authoritative**: Brain recomputes/validates `ctwa_candidate` vs ordinary from the sanitized CTWA fields before persistence. A mismatch is rejected or normalized deterministically; never trust a free-form classification string from the wire.
- `transport_kind` is transport evidence. `inbound_kind` stays `null` everywhere: it described meaning relative to a lifecycle binding, and nothing derives that any more.
- No contact-global chronological inference. Brain never labels the oldest CTWA event for a phone as an origin, and `conversation_context()` is bounded in window and count precisely so its answer stays context rather than an attribution history.
- Observer outbox contains no raw message text, raw JID/LID, raw observer message ID, raw source ID, raw ctwaClid, or full URL. Safe HMAC/length/hostname metadata only, plus sanitized display name until its 24h expiry.
- Retention is enforced on the ingestion path, not by a scheduler. A limit whose enforcement depends on a loop nobody wired is not enforced; that is exactly how both limits went unapplied until 2026-08-31.
- Brain holds no FamaChat credential. Reading and writing client state is Reno's, through its own allowlisted MCP tools.
- Reno's status writes always carry `expectedStatus`, so FamaChat refuses a write over a value a human changed. Only forward transitions are valid.
- CEO uses one `conversation_context()` capability; worker `conversation_phone()` remains fallback only for Porteiro/Cadastro.
- Reno first new-lead history lookup is exactly one `conversation_recent()` call in that turn; an error is recorded, not retried in the same turn.
- No backfill.

## Execution Order

### Stage 0 — Clean baselines

- [x] Record current Brain and operational Hermes repo HEAD/status, into
  `/var/lib/brain/runtime/stage0-baseline.json` rather than into a conversation.
  The command is in `docs/runbook.md`; every `dirty_lines` must be `0`.
- [x] Record installed upstream Hermes version/HEAD/status and critical-file hashes.
- [x] Confirm current Hermes WhatsApp health/queue and Brain health.
- [x] Do not pair/restart/change any upstream Hermes component as part of
  implementation setup. Restarting `hermes-gateway` to load a Fama-owned plugin
  is not a change to upstream, and the integrity gate is what proves it.

**Audited 2026-09-01.** Three defects were found behind boxes that were already
ticked. There was no durable baseline at all — the record existed only in a
conversation, so nothing could be compared against it later. `/root/.hermes`
had five untracked skill-curator backup directories, so its status was never
clean and its HEAD never described the machine; they are now ignored. And
`ctwa-shadow-results.json` remained in the runtime directory, holding gate
verdicts for an architecture that no longer exists, feeding a script that had
been deleted.

### Stage 1 — Brain transport/context foundation

- [x] Execute all tasks in `2026-08-29-ctwa-brain-transport-context.md` using TDD.
- [x] Validate the context contract with `transport_kind` present and `inbound_kind=null`.
- [x] Prove retention actually runs: an expired display name is deleted from storage, not merely hidden from reads.
- [x] Brain deploys no status writer and holds no FamaChat credential.
- [ ] **Not closed:** the code has never run in production. Per the Completion
  Definition below, a stage is complete when its component has processed a real
  case end to end, and Stage 1's code sits in a branch while the machine runs
  the previous release.

**Gate:** component plan 1 all PASS, upstream Hermes untouched.

**Audited 2026-09-01.** Twelve of the thirteen acceptance gates pass against
tests; `HERMES_COMPATIBILITY_CHECK` fails against the deployed principal and
passes against the post-deploy bundle, which is the correct reading of a change
that has not been installed. Three defects were found and fixed:

- The observer's own suite had never been run after the Python refactor, and it
  was the only thing exercising the Node-to-Python contract. `RuntimeIds` had
  lost an argument and the cross-language probe had been failing for a day
  while all 386 Python tests stayed green. The runbook now requires that suite.
- `phone_for_contact_key` survived in `whatsapp_identity.py`: it existed only
  for the writer's claim contact proof, had no caller left outside its own
  tests, and was the last FamaChat reference anywhere in `src/`.
- Both were invisible to every gate, because a gate only ever checks what
  someone thought to point it at.

### Stage 2 — Integrity checker before first production deployment

- [x] Capture `/var/lib/brain/runtime/hermes-integrity-baseline.json` before installing/restarting any Brain observer/plugin change on the VPS.
- [x] Verify baseline immediately; must PASS.

**Gate:** `HERMES_ORIGINAL_INTEGRITY=PASS`.

### Stage 3 — Production observer in capture-only mode

- [x] Execute `2026-08-29-ctwa-whatsapp-observer.md` completely.
- [x] Install `brain-whatsapp-observer.service` with its own session and secrets.
- [x] Pair it manually as a second linked device; never disconnect Hermes device.
- [x] Repeat one controlled CTWA and require Brain safe ingest plus a served `conversation_context()`.
- [x] Re-run upstream integrity verify.

**Gate:** component plan 2 acceptance gate + real `CONVERSATION_CONTEXT_E2E=PASS`; no CRM write capability enabled.

### Stage 4 — Operational Profile contracts and least privilege

- [ ] Execute `2026-08-29-ctwa-hermes-profile-contracts.md` in the operational Hermes repo.
- [ ] Generate Reno exact FamaChat allowlist from live MCP `tools/list`; no wildcard.
- [ ] Add `fc_patch_clientes_by_id` to Reno with the mandatory-`expectedStatus` contract, and record that this reverses a deliberate Stage 4 removal.
- [ ] Run `verify_team.py core` then `full`.
- [ ] Re-run upstream integrity verify after any gateway/Profile restart required to load Fama-owned changes.

**Gate:** component plan 3 acceptance gate, upstream untouched.

### Stage 5 — Deploy the hook-free CEO plugin

- [ ] Update `/etc/brain/brain.toml` so the `default` principal grants only `conversation_context`. The file is read at startup, so the edit itself changes nothing; the constraint is that code, plugin and config must all be in place before the restart, because whichever is stale at that moment is what fails.
- [ ] Replace the installed `/root/.hermes/plugins/brain-ceo-bridge` with the hook-free version.
- [ ] Confirm the gateway loads it and registers exactly one tool and zero hooks.
- [ ] Run one controlled CTWA and confirm the CEO receives contact-scoped context.
- [ ] Re-run upstream integrity verify.

**Gate:** no hook is registered by this plugin, and `conversation_context()` answers from a real inbound.

## Required controlled E2E matrix

```text
CTWA_ONLY_T0=PASS
SECOND_CTWA_ATTRIBUTED_NOT_HUMAN=PASS
JA_E_CLIENTE_CONTRACT=PASS
CORRETOR_ATIVO_CONTRACT=PASS
CADASTRO_READBACK_FAIL_STOPS_HANDOFF=PASS
OBSERVER_REPLAY_DEDUP=PASS
RETENTION_ACTUALLY_PURGES=PASS
MANUAL_CRM_STATE_NEVER_DOWNGRADED=PASS
```

The last one changed subject with Amendment 2: it now proves that Reno's write carries `expectedStatus` and that FamaChat rejects a stale predicate, which is the whole protection standing between a model and a human's edit. Seven scenarios were dropped because they described lifecycle-engine semantics that no longer exist.

## Final Gate

Every value below must be PASS:

```text
OBSERVER_COEXISTENCE
RAW_CTWA_CAPTURE
CONVERSATION_CONTEXT_E2E
CADASTRO_READBACK
RENO_FIRST_HISTORY
RENO_CONDITIONAL_STATUS_WRITE
RETENTION_ENFORCED
HERMES_COMPATIBILITY
HERMES_ORIGINAL_INTEGRITY
```

Evidence must be current, not merely present. A PASS derived from a run older than the change it claims to cover is `STALE` and blocks exactly like a failure: on 2026-08-31 a gate reported PASS from the previous day's rows while the mechanism producing them was dead.

## Completion Definition

Implementation is complete only when:

- all three component plans' applicable tasks are checked and committed;
- all unit/integration/operational verification suites pass;
- observer secrets/session paths have correct permissions;
- upstream Hermes integrity is unchanged from pre-deployment baseline;
- each component has processed at least one real production case end to end. A stage is not complete because its tests pass: the lifecycle shadow stage of the previous plan was marked done with an engine that had never created a single lifecycle.
