# Hermes compatibility repair implementation plan

**Goal:** Restore the installed CEO Brain context plugin and make post-update checks usable on Hermes 25761bb221.
**Architecture:** Keep Hermes upstream untouched. Deploy the reviewed plugin already in Brain; repair stale imports and inspect the runtime owners of moved methods. Verify notification identity through ContextVars and a temporary SQLite database. Preserve old integrity evidence before capturing the verified new checkout.
**Tech stack:** Python 3.11, unittest, Hermes native plugin APIs, systemd.
**Spec:** `/root/brain/docs/runbook.md`, authorized repair following the September 7 audit.

- [ ] Reproduce installed bridge rejection using BrainService._context_event and run existing plugin tests against the versioned source.
- [ ] Add regression coverage for current upstream contracts and additive Brain health fields; observe failures.
- [ ] Update scripts/hermes_integration_check.py to current import and method owners, replace brittle subscription string matching with a real isolated subscription probe.
- [ ] Update scripts/smoke_test.py to validate required health fields while allowing optional extensions; keep incompatible required fields failing.
- [ ] Run full Python suite, lint, formatting, installed-Hermes contract checks, and full team verification.
- [ ] Back up installed plugin and checkers, install the tested files, restart only CEO using hermes gateway restart, verify health and loaded PID.
- [ ] Preserve old baseline, confirm upstream matches the recorded successful update, capture and verify a new baseline, rerun smoke checks.
