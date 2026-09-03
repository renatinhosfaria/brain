# Task 6 report: Brain lifecycle, context projection, and health

## RED evidence

Added `tests/test_meta_context_integration.py` and extended the existing health
expectations in `tests/test_brain.py` before implementation. The initial focused
run failed at the intended contract boundaries:

```text
AttributeError: brain.service does not have the attribute RemoteMetaAdsMcpClient
AssertionError: ... 'meta_ads_mcp': 'disabled' ...
```

This proved that Brain did not yet own a lifecycle-safe shared Meta client and
that the health projection lacked the required additive state.

## GREEN evidence

`BrainService` initializes the writable runtime schema before it constructs
the Meta client/service. Construction is local; it makes no remote operation.
The enabled client is shared by the context and worker paths, while the
existing transport staging boundary remains unchanged. `close()` is idempotent
and the MCP lifespan invokes it only after both housekeeping tasks have been
cancelled and awaited.

Context now left-joins durable attribution rows. CTWA events emit only the
safe contract: five exact confirmation fields for confirmed state, or
status/reason for pending and unavailable state. Ordinary events have no Meta
field. Before its final read, context may resolve exactly the newest pending
CTWA event inside the current six-hour context window, using the lesser of
1.5 seconds and the remaining context deadline. Any resolution failure or
expired budget leaves the normal raw context intact.

The worker uses the configured interval, runs in `asyncio.to_thread`, contains
housekeeping errors, and honors the durable auth circuit before probing or
processing its normal bounded Task 5 batch. Health is additive: Meta can be
`disabled`, `ready`, or `degraded` without making a healthy Brain unavailable.

Focused verification:

```text
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_meta_context_integration tests.test_meta_attribution \
  tests.test_gateway_api tests.test_brain -q
Ran 98 tests in 16.464s
OK

.venv/bin/ruff check .
All checks passed!

git diff --check
exit 0
```

The transport-focused suite also passed:

```text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest -q
Ran 189 tests in 16.701s
OK
```

Full verification:

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
Ran 586 tests in 55.123s
OK
```

The full suite emitted only its established ephemeral cursor-secret diagnostics
and retention-fixture warnings.

## Changed files

- `src/brain/service.py`
  - Initializes the shared Meta attribution/client lifecycle after runtime
    schema initialization, adds additive health, safe close, bounded newest
    pending context resolution, and safe left-join projection.
- `src/brain/mcp_server.py`
  - Adds the configured-interval Meta worker and deterministic task
    cancellation/close during wrapped MCP lifespan shutdown.
- `src/brain/meta_attribution.py`
  - Makes a worker tick respect disabled mode and a durable open auth circuit,
    then lazily probe before its existing bounded due-job batch.
- `tests/test_brain.py`
  - Updates the existing health contract for `meta_ads_mcp`.
- `tests/test_meta_context_integration.py`
  - Covers disabled/no-network startup, additive health, projection states,
    newest-only on-demand resolution, fail-open deadline/error behavior,
    no sensitive audit leakage, auth-circuit tick behavior, and lifespan
    tick/cancel/close behavior.

## Self-review

- No remote call is made by `BrainService.__init__` or health.
- Context and worker share one Brain-owned `RemoteMetaAdsMcpClient`, whose
  existing session lock serializes their remote operations.
- The lookup query stays within the existing six-hour/context-count scope and
  resolves a single newest pending CTWA event only.
- The durable Task 5 revision/auth recovery and Task 4 short-transaction
  boundaries remain authoritative: all remote work remains outside SQLite
  writes, and the worker does not bypass an open durable auth circuit.
- No source ID, API key, raw remote payload, or unconfirmed ad/campaign name
  is added to context or audit logs. Remote/context failures are fail-open.
- `mcp-meta-ads` was not modified and no live credential or remote probe was
  used.

## Concerns and rulings

- The existing gateway API has no separate transport-level request-deadline
  parameter. The service treats the configured context budget as the request
  deadline, then caps the Meta slice at 1.5 seconds. This is bounded and never
  extends the synchronous gateway request.
- The pre-existing `TransportService` continues to own ingestion-time staging.
  Brain's dedicated attribution instance is intentionally used only for
  context and worker lifecycle, so those two paths share the required client
  lock without changing Task 5's ingestion transaction boundary.

## Fix round 1: absolute deadline and shutdown join

### RED evidence

Added three focused regressions before changing production code:

- `resolve_source(..., deadline=...)` was not accepted, which showed that the
  context's absolute deadline was discarded and replaced by a fresh relative
  budget after its SQLite work:

  ```text
  TypeError: MetaAttributionService.resolve_source() got an unexpected
  keyword argument 'deadline'
  ```

- A fake probe advancing the monotonic clock beyond the deadline exposed that
  the following ad call could still receive the old deadline allowance.
- A blocking fake worker tick showed shutdown called `service.close()` before
  the tick thread was released:

  ```text
  AssertionError: True is not false
  ```

### Implementation

- `gateway_conversation_context` creates one absolute monotonic deadline
  capped at 1.5 seconds. It passes that unchanged through the newest-pending
  context resolver, which checks it again after its SQLite lookup.
- `resolve_pending_for_contact` and `resolve_source` accept the optional
  absolute `deadline` keyword. Reads, the lease claim, the durable probe-state
  read, probe, ad, campaign, and completion boundary re-check remaining time;
  no remote operation begins once it is expired. The worker keeps its existing
  configured relative budget by creating its own absolute deadline only when
  no deadline is supplied.
- `BrainMCPServer` keeps the `asyncio.to_thread` tick in its own task and
  shields it from cancellation. Lifespan shutdown cancels the loop, then joins
  any in-flight tick before calling `service.close()`, preventing concurrent
  use/close of the shared client.

### Verification

```text
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_meta_attribution tests.test_meta_context_integration \
  tests.test_gateway_api tests.test_brain -q
Ran 101 tests in 26.601s
OK

PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest -q
Ran 189 tests in 16.586s
OK

.venv/bin/ruff check .
All checks passed!

git diff --check
exit 0

PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
Ran 589 tests in 74.533s
OK
```

The full suite emitted only its established ephemeral cursor-secret diagnostics
and retention-fixture warnings.

### Fix-round self-review and ruling

- The request deadline is never reconstructed from a remaining duration after
  a SQLite operation: nested context resolver calls receive the original
  monotonic timestamp, and each remote boundary checks it again.
- Python cannot force-cancel a running `to_thread` worker. The safe ruling is
  to cancel its scheduling loop, shield and join the already-started tick, and
  only then close the shared client. This can delay shutdown by the bounded
  Task 5 worker operation, but it removes the use-after-close race.
