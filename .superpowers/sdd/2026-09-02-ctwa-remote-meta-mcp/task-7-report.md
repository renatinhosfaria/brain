# Task 7 — CEO bridge contract

## RED/GREEN

- RED: added confirmed, pending/unavailable, ordinary-event, unknown-key,
  inactive-status, malformed-ID, unconfirmed-name, token-shaped,
  remote-shaped, and bounds fixtures. The three valid fixtures failed closed
  before the bridge validator existed.
- GREEN: added the exact `meta_attribution` event allowlist branches and a
  fail-closed validator. All fixtures pass.

## Changed files

- `integrations/hermes/brain-ceo-bridge/tools.py`
- `integrations/hermes/brain-ceo-bridge/README.md`
- `tests/test_ceo_bridge_plugin.py`

## Commands/output

- `./.venv/bin/python -m unittest -q tests.test_ceo_bridge_plugin` — `Ran 21 tests ... OK`
- `/tmp/brain-uv-bin/uv run --offline ruff check integrations/hermes/brain-ceo-bridge/tools.py tests/test_ceo_bridge_plugin.py` — `All checks passed!`
- `./.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` — `Ran 592 tests in 53.773s; OK`

## Self-review

- Confirmed attribution accepts exactly five public fields, decimal IDs
  bounded to 64 digits, and bounded printable safe names.
- Pending/unavailable accepts exactly `status` and a bounded safe `reason`.
- Attribution is accepted only on `ctwa_candidate`; ordinary events and
  unknown/token/remote-shaped fields fail closed without echoing payload data.
- Existing session, phone, raw-value, response-size, and fail-closed paths
  were left unchanged.

## Concerns/rulings

- Safe Meta names allow printable ASCII spaces (needed for normal ad names),
  while rejecting control characters and template interpolation markers.
- Full-suite output includes pre-existing warnings about an ephemeral cursor
  secret and simulated retention failures; the suite still completed OK.

## Round 1 fixes

- Updated pending/unavailable validation to accept either status-only or
  status plus one bounded safe reason, matching Brain's legitimate
  `{"status":"pending"}` projection.
- Added fixtures for status-only cases and missing/malformed status,
  missing confirmed fields, malformed IDs, unsafe and oversized names/reasons,
  with fail-closed and no-echo assertions.
- Updated README contract wording to make the reason optional.

Validation after fixes:

- `./.venv/bin/python -m unittest -q tests.test_ceo_bridge_plugin` — 21 passed.
- `/tmp/brain-uv-bin/uv run --offline ruff check integrations/hermes/brain-ceo-bridge/tools.py tests/test_ceo_bridge_plugin.py` — all checks passed.
- `./.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` — 592 passed in 62.513s (same pre-existing warnings noted above).
