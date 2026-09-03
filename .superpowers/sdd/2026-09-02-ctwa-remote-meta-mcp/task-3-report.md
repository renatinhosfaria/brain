# Task 3 report: read-only remote Meta Ads MCP client

## RED evidence

Added `tests/test_meta_ads_mcp.py` before `src/brain/meta_ads_mcp.py`. The
focused run failed because the production module did not yet exist:

```text
ModuleNotFoundError: No module named 'brain.meta_ads_mcp'
```

During security review, added a regression test for the installed MCP
Streamable HTTP transport's DEBUG logger. It failed before the safeguard,
proving that a raw-looking source/payload message could reach a handler:

```text
AssertionError: ['source_id=101 remote_payload=untrusted'] != []
```

## GREEN evidence

Implemented the private event-loop-thread facade, fake-session injection,
single-session lifecycle/recreation, response-byte budget transport, strict
three-tool allowlist, probe, structured parsing, bounded error mapping, and
SDK transport-log suppression. Then ran:

```text
PYTHONPATH=src /root/brain/.venv/bin/python -m unittest tests.test_meta_ads_mcp -v
Ran 12 tests in 0.109s
OK

/root/brain/.venv/bin/ruff check .
All checks passed!

/root/brain/.venv/bin/ruff format --check src/brain/meta_ads_mcp.py tests/test_meta_ads_mcp.py
2 files already formatted

PYTHONPATH=src /root/brain/.venv/bin/python -m unittest discover -s tests -q
Ran 533 tests in 44.320s
OK
```

The full suite emits existing cursor-secret and retention-test diagnostic
warnings; it exited with status 0.

## Changed files

- `src/brain/meta_ads_mcp.py`
- `tests/test_meta_ads_mcp.py`

## Self-review

- The HTTP client sends only the configured bearer header and exact endpoint,
  with configured timeout, redirects disabled, and `trust_env=False`.
- The event loop owns at most one HTTP client/session at once; its async lock
  serializes initialization and tool operations. Transport/protocol/auth
  failures close resources, and a later call recreates them.
- Only `META_READ_TOOLS` can reach `call_tool`; public paths send exactly the
  account-list, ad, and campaign calls required by the brief.
- Probe requires all three tools and exactly one structured
  `{"data": [{"id": "act_1598606388477916"}]}` account result.
- Structured content is the only parsed result source. The client returns
  code-only `MetaAdsError`s and suppresses the SDK logger that serializes MCP
  messages at DEBUG, keeping keys and source/ad/campaign payloads out of its
  logs and exceptions.
- The counting transport rejects a response at the declared-size boundary or
  before an over-limit chunk is yielded; its shared per-operation budget maps
  to `meta_invalid_response`.
- No live key or remote repository was used or changed. No Task 1/2 files
  were modified.

## Rulings

- `deadline` is interpreted as an absolute `time.monotonic()` deadline, so it
  consumes the caller's remaining request budget rather than adding a new
  timeout.
- `invalidate()` closes the local session and resets the local authentication
  circuit. This gives the owning service an explicit recovery action after an
  operator rotates credentials; ordinary calls never retry or try another
  credential.

## Concerns

`ruff format --check .` reports 18 pre-existing, out-of-scope files that would
be reformatted. The changed Task 3 files are formatted, and repository-wide
`ruff check .` passes.

## Round 1 review fixes

### RED evidence

Added focused regressions for concurrent request/close/invalidate lifecycle
handling, teardown exceptions, 403 authentication, malformed/incomplete/account
mismatch session recreation, and real Streamable HTTP cleanup. Before the fix,
the focused run demonstrated all reviewed failures:

```text
RuntimeError: Event loop is closed
AssertionError: 'DELETE' unexpectedly found in ['POST', 'POST', 'GET', 'DELETE']
RuntimeError: untrusted remote payload
```

It also showed that `meta_incomplete_result` and `meta_account_mismatch` left
the old fake session open.

### GREEN evidence

The sync facade now guards lifecycle state with a thread lock, tracks/cancels
in-flight request futures before shutdown, and maps cancellation/stopped-loop
races to `meta_server_unavailable`. `invalidate()` and `close()` contain
teardown failures. The client now composes the installed
`StreamableHTTPTransport` in post-only mode: it starts neither the SDK's
background GET stream/retry loop nor its optional cleanup DELETE.

Malformed, incomplete, and account-mismatch results discard the session before
the next call. The test transport proves the post-only session sends neither
`GET` nor `DELETE`; 401 and 403 tests both open the local auth circuit.

```text
PYTHONPATH=src /root/brain/.venv/bin/python -m unittest tests.test_meta_ads_mcp -v
Ran 18 tests in 0.260s
OK

/root/brain/.venv/bin/ruff check .
All checks passed!

/root/brain/.venv/bin/ruff format --check src/brain/meta_ads_mcp.py tests/test_meta_ads_mcp.py
2 files already formatted

PYTHONPATH=src /root/brain/.venv/bin/python -m unittest discover -s tests -q
Ran 539 tests in 47.701s
OK
```

The repository-wide formatter still reports the same 18 pre-existing,
out-of-scope files. No credentials, source/ad/campaign payloads, or remote
repository files were used or changed.
