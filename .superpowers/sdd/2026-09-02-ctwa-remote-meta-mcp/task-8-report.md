# Task 8 report: probe, deployment, and operator runbook

## RED evidence

Added `tests/test_meta_ads_probe.py` and the deployment contract test before
the probe implementation. The focused run failed with four
`ModuleNotFoundError` errors because `scripts/meta_ads_mcp_probe.py` did not
exist; the deployment assertion passed. This demonstrated the tests were
exercising the missing behavior.

## GREEN evidence

Implemented the thin probe around the existing `BrainSettings.from_env` and
`RemoteMetaAdsMcpClient.probe()` APIs. Focused verification:

```text
PYTHONPATH=src .../python -m unittest tests.test_meta_ads_probe tests.test_deployment_contracts
Ran 35 tests ... OK
```

Full Python verification:

```text
PYTHONPATH=src .../python -m unittest discover -s tests
Ran 597 tests in 75.707s
OK
```

Lint and whitespace verification:

```text
ruff check scripts/meta_ads_mcp_probe.py tests/test_meta_ads_probe.py tests/test_deployment_contracts.py
All checks passed!
git diff --check
```

## Changes

- Created `scripts/meta_ads_mcp_probe.py`: no CLI options, shared settings and
  client, content-free `disabled`, exact-account `ready`, or bounded error
  output; exit 0 only for disabled/success and 1 for failures.
- Added unit coverage for disabled, ready, bounded failure, and secret-safe
  invalid-argument handling.
- Extended deployment contracts for disabled-by-default TOML, root-only secret
  environment, UMask, and unchanged service path boundary.
- Updated deployment service comments, README, and runbook with the exact
  rollout, health/CTWA verification, disable rollback, and key rotation steps.
- Existing `deploy/brain.env.example` and `deploy/brain.toml.example` already
  contained the required disabled default, exact account, HTTPS URL, and
  commented root-only API-key placeholder; no unnecessary changes were made.

## Self-review, concerns, and rulings

- The probe catches configuration errors as `config_invalid` and unexpected
  client failures as `meta_server_unavailable`; it never prints exception
  text or remote payloads.
- Argument rejection is deliberately manual because argparse diagnostics can
  echo a user-supplied secret. The command accepts no URL or credential
  arguments.
- No live credentials, network calls, `mcp-meta-ads` edits, or additional
  systemd filesystem/network permissions were used.
- The client remains responsible for all HTTP/MCP logic and bounded error
  mapping, as required.

## Round-1 fix report

### RED

Added focused cases for nonzero disabled status, constructor failure,
unexpected probe failure, and cleanup failure. Before the fix, the disabled
assertion failed (`0 != 1`) and constructor/close failures raised unbounded
`RuntimeError: secret` tracebacks.

### GREEN

The probe now returns 1 for `disabled`, catches ordinary exceptions around
settings, client construction, probing, and cleanup, and keeps successful
output/exit status stable if cleanup fails. Verification:

```text
ruff check ...
All checks passed!
Ran 38 tests ... OK
Ran 600 tests in 80.247s
OK
```

### Self-review

No API/URL arguments, live credentials, network calls, or payload output were
introduced. `BaseException` remains uncaught intentionally so process-level
termination signals retain normal behavior; all ordinary exceptions are
mapped to content-free bounded codes.
