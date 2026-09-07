# Hermes compatibility repair implementation plan

**Goal:** Restore the installed CEO Brain context plugin and make post-update checks usable on Hermes 25761bb221.
**Architecture:** Keep Hermes upstream untouched. Deploy the reviewed plugin already in Brain; repair stale imports and inspect the runtime owners of moved methods. Verify notification identity through ContextVars and a temporary SQLite database. Preserve old integrity evidence before capturing the verified new checkout.
**Tech stack:** Python 3.11, unittest, Hermes native plugin APIs, systemd.
**Spec:** `/root/brain/docs/runbook.md`, authorized repair following the September 7 audit.

- [x] Reproduce installed bridge rejection using BrainService._context_event and run existing plugin tests against the versioned source.
- [x] Add regression coverage for current upstream contracts and additive Brain health fields; observe failures.
- [x] Update scripts/hermes_integration_check.py to current import and method owners, replace brittle subscription string matching with a real isolated subscription probe.
- [x] Update scripts/smoke_test.py to validate required health fields while allowing optional extensions; keep incompatible required fields failing.
- [x] Run full Python suite, lint, formatting, installed-Hermes contract checks, and full team verification.
- [x] Back up installed plugin and checkers, install the tested files, restart only CEO using hermes gateway restart, verify health and loaded PID.
- [x] Preserve old baseline, confirm upstream matches the recorded successful update, capture and verify a new baseline, rerun smoke checks.

## Deployment evidence

Completed September 7, 2026. Python suite: 626 tests passed; Ruff lint and formatting passed. Installed plugin rejects the current Brain event before deployment and accepts it afterwards. Integration checker, smoke checker, full team verifier and refreshed integrity baseline all pass. Hermes upstream remains clean at `25761bb2214fb80d1cc1196e4d72ab3d8bc80440`, matching the successful update receipt.

Only CEO gateway restarted gracefully: PID 1553055 → 1559510. WhatsApp and Telegram reconnected. Backup: `/var/lib/brain/runtime/repairs/20260907T212033Z-hermes-compat`. Old baseline: `/var/lib/brain/runtime/hermes-integrity-baseline.json.20260907T212329Z.superseded`. No live messages were used as test input.

Independent review caught project inheritance in the temporary subscription probe. Explicit `project_id=""` suppresses inheritance; regression coverage simulates a project-linked board and requires no projects database access. Existing observer warning (`unresolved_identity_count=1`) was outside this repair.
