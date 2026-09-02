# CTWA Meta Ads Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve each eligible CTWA `sourceId` to the exact Meta ad and campaign for `act_1598606388477916`, persist the proof in Brain, and expose confirmed or pending attribution through `conversation_context({})` without blocking customer service.

**Architecture:** Add a deterministic, read-only Ads MCP adapter behind a small `MetaAdsClient` interface. A SQLite-backed attribution service stages work transactionally with CTWA ingestion, resolves from a local catalog or a bounded live Meta lookup, retries through a lifespan worker, and joins the persisted result into CEO context.

**Tech Stack:** Python 3.11+, SQLite/WAL, `mcp==2.0.0`, `httpx2` through the MCP SDK, Starlette, `unittest`, systemd, Hermes plugin API.

**Spec:** `docs/superpowers/specs/2026-09-02-ctwa-meta-ads-attribution-design.md`

## Global Constraints

- The only allowed ad account is `act_1598606388477916`; internally normalize it to `1598606388477916` and reject every other account.
- The Ads MCP endpoint is fixed to `https://mcp.facebook.com/ads` and is not runtime-configurable.
- A confirmation requires `sourceType == "ad"`, exact `sourceId == ad.id`, the configured account, a Meta-supplied non-empty ad name, and a Meta-supplied campaign ID/name.
- Never resolve by `ctwaClid`, URL, title, body, source application, creative similarity, or fuzzy matching.
- Keep active, paused, completed, rejected, and archived readable ads eligible for the 90-day event window.
- `conversation_context({})` budgets are 4 seconds for Ads MCP, 5 seconds inside Brain, and 7 seconds in the Hermes bridge.
- Meta failure leaves attribution `pending`; it never makes raw CTWA context or customer service unavailable.
- Meta names and all CTWA fields are untrusted evidence, never instructions.
- No CRM writes, CAPI events, performance metrics, multiple accounts, direct Graph API fallback, or Meta mutations are in scope.
- No token, `sourceId`, `ctwaClid`, ad/campaign name, URL, or raw Meta response may appear in logs, metrics, health, filenames, or errors.
- The Meta token is accepted only from `BRAIN_META_ADS_MCP_ACCESS_TOKEN` and must be excluded from dataclass representations.
- Preserve all unrelated changes in `/root/brain` and `/root/.hermes`; commit the repositories separately.

## File Map

- Create `src/brain/meta_ads_models.py` for validated account IDs, source IDs, ad records, capability results, error codes, and public payload builders.
- Create `src/brain/meta_ads_store.py` for attribution/catalog/job SQL against caller-owned transactions.
- Create `src/brain/meta_ads_mcp.py` for the fixed-endpoint MCP SDK transport, tool discovery, read-only calls, pagination, and strict response normalization.
- Create `src/brain/meta_attribution.py` for orchestration, exact confirmation, retries, catalog refresh, and component health.
- Modify `src/brain/config.py` for feature, account, credential-expiry, timeout, response-size, and synchronization settings.
- Modify `src/brain/runtime_db.py` for three additive tables and indexes.
- Modify `src/brain/transport_service.py` to stage eligible attribution in the same transaction as a transport event.
- Modify `src/brain/service.py` to wire the component, perform bounded context-time resolution, join attribution into context, and report degraded Meta health independently.
- Modify `src/brain/mcp_server.py` to run the background attribution loop in the existing application lifespan.
- Create `scripts/meta_ads_mcp_probe.py` for a non-sensitive authenticated capability/account/known-ad check.
- Create `scripts/install_meta_ads_credential.py` for atomic root-only token installation and rotation without command-line or stdout exposure.
- Create `tests/test_meta_ads_models.py`, `tests/test_meta_ads_store.py`, `tests/test_meta_ads_mcp.py`, and `tests/test_meta_attribution.py`.
- Modify `tests/test_config.py`, `tests/test_runtime_db.py`, `tests/test_transport_ingest.py`, `tests/test_brain.py`, `tests/test_gateway_api.py`, `tests/test_ceo_bridge_plugin.py`, and `tests/test_deployment_contracts.py`.
- Modify `integrations/hermes/brain-ceo-bridge/tools.py` and `integrations/hermes/brain-ceo-bridge/README.md`.
- Modify `deploy/brain.env.example`, `deploy/brain.toml.example`, `README.md`, and `docs/runbook.md`.
- Modify the installed `/root/.hermes/plugins/brain-ceo-bridge/tools.py` and `/root/.hermes/SOUL.md` only after the versioned bridge passes.

---

### Task 1: Domain types and fail-safe configuration

**Files:**
- Create: `src/brain/meta_ads_models.py`
- Create: `tests/test_meta_ads_models.py`
- Modify: `src/brain/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `canonical_account_id(value: object) -> str`, `eligible_source(raw: object) -> ObservedAttribution | None`, `MetaAdRecord`, `MetaAdsCapabilities`, `MetaAdsError`, `confirmed_payload(record, observed, confirmed_at)`, and `pending_payload(observed, last_attempt_at, retry_scheduled, last_error_code)`.
- Produces settings: `meta_attribution_enabled`, `meta_ad_account_id`, `meta_ads_mcp_access_token`, `meta_ads_mcp_token_expires_at`, `meta_ads_mcp_timeout_seconds`, `meta_ads_mcp_response_max_bytes`, `meta_ads_sync_interval_seconds`, `meta_ads_full_sync_interval_seconds`.
- Consumes no Meta network response; all fixtures are synthetic.

- [ ] **Step 1: Write failing identifier and eligibility tests**

```python
import unittest

from brain.meta_ads_models import canonical_account_id, eligible_source


class MetaAdsModelTests(unittest.TestCase):
    def test_canonical_account_is_pinned_and_normalized(self):
        self.assertEqual(
            canonical_account_id("act_1598606388477916"), "1598606388477916"
        )
        self.assertEqual(
            canonical_account_id("1598606388477916"), "1598606388477916"
        )
        for foreign in ("act_1", "1598606388477917", " act_1598606388477916"):
            with self.subTest(foreign=foreign), self.assertRaises(ValueError):
                canonical_account_id(foreign)

    def test_only_decimal_ad_source_is_eligible(self):
        observed = eligible_source(
            {
                "sourceType": "ad",
                "sourceId": "120200000000001",
                "ctwaClid": "click-evidence",
            }
        )
        self.assertEqual(observed.source_id, "120200000000001")
        self.assertEqual(observed.ctwa_clid, "click-evidence")
        self.assertIsNone(
            eligible_source({"sourceType": "post", "sourceId": "1202"})
        )
        self.assertIsNone(
            eligible_source({"sourceType": "ad", "sourceId": "12_02"})
        )
```

Cover 1 and 64 digits, reject empty/65-digit/non-ASCII digits, and bound names
to 512 UTF-8-safe characters. Also assert a `MetaAdsError`
contains only its approved code and optional numeric `retry_after_seconds`, not
the supplied fixture value.

- [ ] **Step 2: Run the model tests to verify RED**

Run:

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_models -v
```

Expected: FAIL with `ModuleNotFoundError: brain.meta_ads_models`.

- [ ] **Step 3: Implement immutable domain types and public shapes**

Use these signatures:

```python
ALLOWED_ACCOUNT_ID = "1598606388477916"
META_ADS_MCP_URL = "https://mcp.facebook.com/ads"
META_ERROR_CODES = frozenset({
    "meta_timeout",
    "meta_rate_limited",
    "meta_server_unavailable",
    "meta_auth_unavailable",
    "meta_required_tool_unavailable",
    "meta_not_found",
    "meta_incomplete_result",
    "meta_account_mismatch",
    "meta_invalid_response",
})

@dataclass(frozen=True)
class ObservedAttribution:
    source_id: str
    ctwa_clid: str | None

@dataclass(frozen=True)
class MetaAdRecord:
    account_id: str
    ad_id: str
    ad_name: str
    ad_status: str | None
    ad_effective_status: str | None
    adset_id: str | None
    adset_name: str | None
    adset_status: str | None
    campaign_id: str
    campaign_name: str
    campaign_status: str | None
    creative_id: str | None
    creative_name: str | None
    metadata_complete: bool
    fetched_at: float

@dataclass(frozen=True)
class MetaAdsCapabilities:
    account_id: str
    required_tools: frozenset[str]
    account_argument: str
    entity_selector_argument: str
    result_array_path: tuple[str, ...]
    exact_id_filter_supported: bool

class MetaAdsError(Exception):
    def __init__(self, code: str, retry_after_seconds: float | None = None):
        if code not in META_ERROR_CODES:
            raise ValueError("unsupported Meta Ads error code")
        if retry_after_seconds is not None and (
            not math.isfinite(retry_after_seconds) or retry_after_seconds < 0
        ):
            raise ValueError("invalid retry delay")
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds

@dataclass(frozen=True)
class MetaAttributionView:
    event_id: str
    observed: ObservedAttribution
    status: str
    record: MetaAdRecord | None
    confirmed_at: float | None
    last_attempt_at: float | None
    last_error_code: str | None
    retry_scheduled: bool
```

Define the exact public error-code set from spec section 9. Validate every ID,
name, status, finite timestamp, and nullable pairing in `__post_init__`.
Implement `confirmed_payload(record, observed, confirmed_at)` and
`pending_payload(observed, last_attempt_at, retry_scheduled, last_error_code)`
with the exact key sets in spec section 12. Prefer `ad_effective_status` over
`ad_status` for the public `status`.

- [ ] **Step 4: Write failing configuration tests**

Add `unittest` cases that assert:

```python
self.assertFalse(settings.meta_attribution_enabled)
self.assertEqual(settings.meta_ad_account_id, "")
self.assertEqual(settings.meta_ads_mcp_access_token, "")
self.assertNotIn("fixture-token", repr(settings))
self.assertEqual(settings.meta_ads_mcp_timeout_seconds, 4.0)
self.assertEqual(settings.meta_ads_mcp_response_max_bytes, 8 * 1024 * 1024)
self.assertEqual(settings.meta_ads_sync_interval_seconds, 900)
self.assertEqual(settings.meta_ads_full_sync_interval_seconds, 86_400)
```

With `BRAIN_META_ATTRIBUTION_ENABLED=true`, read the exact account, token, and
RFC3339 expiry from the environment. Reject non-boolean feature flags,
foreign/malformed account IDs, non-finite/non-positive timeouts, response limits
below 1 MiB or above 32 MiB, sync intervals below 60 seconds, and full sync
intervals below the incremental interval. Feature-disabled startup must not
require a Meta token. Feature-enabled startup with a missing/expired token must
remain constructible so the Meta component can report degraded independently.

- [ ] **Step 5: Implement strict environment parsing**

Add fields with the token hidden from representations:

```python
meta_attribution_enabled: bool = False
meta_ad_account_id: str = ""
meta_ads_mcp_access_token: str = field(default="", repr=False)
meta_ads_mcp_token_expires_at: float | None = None
meta_ads_mcp_timeout_seconds: float = 4.0
meta_ads_mcp_response_max_bytes: int = 8 * 1024 * 1024
meta_ads_sync_interval_seconds: int = 900
meta_ads_full_sync_interval_seconds: int = 86_400
```

Parse the enable flag only from `true`/`false` or TOML booleans. Parse the
expiry as RFC3339 UTC and retain it internally as Unix seconds. When enabled,
require the pinned account but allow absent/expired credential state to reach
the degraded component rather than aborting the Brain process.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_models tests.test_config -v
.venv/bin/ruff format src/brain/meta_ads_models.py src/brain/config.py tests/test_meta_ads_models.py tests/test_config.py
.venv/bin/ruff check src/brain/meta_ads_models.py src/brain/config.py tests/test_meta_ads_models.py tests/test_config.py
git add src/brain/meta_ads_models.py src/brain/config.py tests/test_meta_ads_models.py tests/test_config.py
git commit -m "feat: define Meta Ads attribution settings"
```

---

### Task 2: Additive schema and transactional attribution store

**Files:**
- Create: `src/brain/meta_ads_store.py`
- Create: `tests/test_meta_ads_store.py`
- Modify: `src/brain/runtime_db.py`
- Modify: `tests/test_runtime_db.py`

**Interfaces:**
- Consumes: Task 1 `ObservedAttribution` and `MetaAdRecord`.
- Produces: `MetaAdsStore.stage_event`, `claim_job`, `fail_job`, `upsert_record_and_confirm`, `context_for_event`, `due_source_ids`, and `purge_catalog`.
- `MetaAdsStore(account_id)` owns the one canonical account value while every method receives a caller-owned connection.
- All store methods accept a caller-owned `sqlite3.Connection`; no method opens or commits a connection.

- [ ] **Step 1: Write failing fresh-schema and migration tests**

Extend `BUSINESS_TABLES` with:

```python
{
    "meta_ads_catalog",
    "ctwa_meta_attributions",
    "meta_attribution_jobs",
}
```

Assert fresh tables expose the columns/checks from spec section 6. Build a
legacy runtime database containing only today's two tables, call
`RuntimeDatabase.initialize()` twice, and assert the three new tables appear
without changing the existing transport row.

- [ ] **Step 2: Run schema tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_db -v
```

Expected: FAIL because the three tables are absent.

- [ ] **Step 3: Add tables, foreign key, checks, and indexes**

Add table definitions to `_SCHEMA`. The attribution table must use:

```sql
event_id TEXT PRIMARY KEY
    REFERENCES transport_events(event_id) ON DELETE CASCADE,
status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')),
CHECK (
    status = 'pending'
    OR (
        matched_ad_id = source_id
        AND match_method = 'source_id_exact'
        AND confirmed_at IS NOT NULL
    )
)
```

Add unique catalog key `(account_id, ad_id)`, unique job key
`(account_id, source_id)`, a due-job index on `(next_attempt_at, lease_until)`,
an attribution lookup index on `(account_id, source_id, status)`, and a catalog
GC index on `last_seen_at`. Keep initialization additive and idempotent.

- [ ] **Step 4: Write failing store state-machine tests**

Use an in-memory/fresh temporary runtime DB and assert this sequence:

```python
store.stage_event(conn, "waevt_one", observed, now=100.0)
assert store.context_for_event(conn, "waevt_one").status == "pending"
assert store.claim_job(conn, observed.source_id,
                       now=101.0, lease_seconds=30.0)
store.upsert_record_and_confirm(conn, record, confirmed_at=102.0)
view = store.context_for_event(conn, "waevt_one")
assert view.status == "confirmed"
assert view.record.ad_id == observed.source_id
```

Also test duplicate staging, two events sharing one job, lease exclusion and
recovery, immutable confirmed binding, rollback, foreign-key cascade,
`meta_not_found` retry scheduling, auth circuit scheduling, catalog refresh
without rebinding, and catalog GC only after 90 days with no retained
attribution.

- [ ] **Step 5: Implement the store with explicit SQL**

Use typed return objects from `meta_ads_models.py`; do not expose `sqlite3.Row`
outside the store. `stage_event` checks for an already Meta-confirmed catalog
row and inserts a confirmed attribution directly; otherwise it inserts pending
plus one deduplicated job. `upsert_record_and_confirm` performs the catalog
upsert and confirms every pending exact `(account_id, source_id)` row in the
same transaction.

Use the retry progression:

```python
RETRY_DELAYS_SECONDS = (60, 300, 900, 3_600, 21_600, 86_400)
```

Cap subsequent retries at 86,400 seconds, apply bounded jitter supplied as an
argument for deterministic tests, and honor a larger valid `Retry-After`.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_db tests.test_meta_ads_store -v
.venv/bin/ruff format src/brain/runtime_db.py src/brain/meta_ads_store.py tests/test_runtime_db.py tests/test_meta_ads_store.py
.venv/bin/ruff check src/brain/runtime_db.py src/brain/meta_ads_store.py tests/test_runtime_db.py tests/test_meta_ads_store.py
git add src/brain/runtime_db.py src/brain/meta_ads_store.py tests/test_runtime_db.py tests/test_meta_ads_store.py
git commit -m "feat: add durable Meta attribution store"
```

---

### Task 3: Read-only Ads MCP client and capability contract

**Files:**
- Create: `src/brain/meta_ads_mcp.py`
- Create: `tests/test_meta_ads_mcp.py`

**Interfaces:**
- Consumes: Task 1 settings and `MetaAdRecord`.
- Produces protocol `MetaAdsClient` with `probe()`, `get_ad(source_id, now)`, and `list_ads(now, full)`.
- Produces implementation `MetaAdsMcpClient` using `mcp.client.streamable_http.streamable_http_client` and `mcp.ClientSession`.

- [ ] **Step 1: Write failing fixed-boundary tests**

Create a fake MCP session factory and assert:

```python
client = MetaAdsMcpClient(settings, session_factory=fake_factory)
capabilities = client.probe()
self.assertEqual(capabilities.account_id, "1598606388477916")
self.assertEqual(capabilities.required_tools, frozenset({
    "ads_get_ad_accounts", "ads_get_ad_entities"
}))
```

Test that the client refuses a session whose `tools/list` omits a required
read tool, provides incompatible input/output schema, or exposes the configured
account zero or two times. Inject a call request containing any tool outside:

```python
READ_TOOLS = frozenset({
    "ads_get_ad_accounts",
    "ads_get_field_context",
    "ads_get_ad_entities",
    "ads_get_creatives",
})
```

and assert it fails locally with `meta_required_tool_unavailable` before the
fake session records a network call.

- [ ] **Step 2: Run client tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_mcp -v
```

Expected: FAIL with `ModuleNotFoundError: brain.meta_ads_mcp`.

- [ ] **Step 3: Implement the MCP SDK session boundary**

Use this session shape inside a private async method and expose synchronous
methods because Brain's gateway already runs service work in a thread:

```python
async with httpx2.AsyncClient(
    headers={"Authorization": f"Bearer {token}"},
    timeout=httpx2.Timeout(timeout_seconds),
    follow_redirects=False,
    trust_env=False,
) as http:
    async with streamable_http_client(
        META_ADS_MCP_URL,
        http_client=http,
        terminate_on_close=True,
    ) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timeout_seconds,
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
```

Run it with `anyio.run` only from a non-event-loop thread. Wrap SDK/HTTP errors
into bounded `MetaAdsError` codes without interpolating request, response,
token, tool content, or exception text. Enforce the 8 MiB ceiling with a
counting `httpx2.AsyncByteStream` wrapper around response bodies and test the
limit at exactly 8 MiB and 8 MiB + 1.

- [ ] **Step 4: Write failing exact lookup and pagination tests**

Use a synthetic structured result containing one foreign ad and one exact ad:

```python
{
    "data": [
        {"id": "120200000000000", "account_id": "1598606388477916"},
        {
            "id": "120200000000001",
            "account_id": "1598606388477916",
            "name": "September lead ad",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "adset": {"id": "1203001", "name": "Prospecting", "status": "ACTIVE"},
            "campaign": {"id": "1204001", "name": "September", "status": "ACTIVE"},
            "creative": {"id": "1205001", "name": "Image A"},
        },
    ],
    "paging": {"cursors": {"after": "next-page"}},
}
```

Assert only the exact ID is returned. Test direct-ID filtering when the live
tool schema declares it; otherwise test bounded pagination and local exact
comparison. Reject text-only tool results, `is_error`, foreign account, wrong
ID, duplicate exact IDs, missing ad/campaign names, unsafe strings, repeated
cursor, page overflow, and oversized collections.

- [ ] **Step 5: Implement schema-checked lookup and normalization**

At probe time, inspect `Tool.input_schema` and `Tool.output_schema`. Accept only
the known account argument names `ad_account_id` or `account_id`, known entity
selectors `entity_type` or `level`, and a structured entity array declared by
the output schema. Store the selected adapter in `MetaAdsCapabilities`; never
let an LLM interpret a schema.

Request only identity and hierarchy fields approved by the spec. Prefer an
exact ID filter declared filterable by `ads_get_field_context`. If it is absent,
page through account ads with a hard maximum of 100 pages and 500 entities per
page, compare IDs locally, and stop as soon as one exact valid record exists.
Require `CallToolResult.structured_content`; never parse free-form text content
into a confirmation.

- [ ] **Step 6: Test error mapping and secret non-disclosure**

Map HTTP/MCP authentication to `meta_auth_unavailable`, throttling plus parsed
`Retry-After` to `meta_rate_limited`, deadline expiry to `meta_timeout`, server
failure to `meta_server_unavailable`, and every incompatible success response
to `meta_invalid_response` or `meta_incomplete_result`. For every branch, use a
fixture token and fixture names and assert neither occurs in exception text or
captured logs.

- [ ] **Step 7: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_models tests.test_meta_ads_mcp -v
.venv/bin/ruff format src/brain/meta_ads_mcp.py tests/test_meta_ads_mcp.py
.venv/bin/ruff check src/brain/meta_ads_mcp.py tests/test_meta_ads_mcp.py
git add src/brain/meta_ads_mcp.py tests/test_meta_ads_mcp.py
git commit -m "feat: add read-only Meta Ads MCP client"
```

---

### Task 4: Resolver, exact confirmation, and retries

**Files:**
- Create: `src/brain/meta_attribution.py`
- Create: `tests/test_meta_attribution.py`

**Interfaces:**
- Consumes: `MetaAdsClient`, `MetaAdsStore`, `RuntimeDatabase`, and Task 1 settings.
- Produces: `MetaAttributionService.stage_event`, `resolve_source`, `resolve_contact_pending`, `run_due_jobs`, `refresh_catalog`, `apply_retention`, `health`, and `tick`.
- Network calls occur outside SQLite transactions; claim/finalize operations use short independent transactions.

- [ ] **Step 1: Write failing cache-hit and exact live-resolution tests**

Use a fake `MetaAdsClient` and temporary runtime DB. Assert:

```python
service.stage_event(conn, event_id="waevt_one", raw=raw_ctwa, now=100.0)
service.resolve_source("120200000000001", now=101.0)
view = runtime.read(lambda conn: store.context_for_event(conn, "waevt_one"))
self.assertEqual(view.status, "confirmed")
self.assertEqual(view.record.ad_id, "120200000000001")
self.assertEqual(fake.calls, [("get_ad", "120200000000001")])
```

Seed a confirmed catalog row, stage a second event for the same `sourceId`, and
assert it confirms transactionally with no client call. Stage two pending
events and assert one live call confirms both.

- [ ] **Step 2: Run resolver tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_attribution -v
```

Expected: FAIL with `ModuleNotFoundError: brain.meta_attribution`.

- [ ] **Step 3: Implement claimed resolution outside the write lock**

Use this sequence:

```python
claimed = runtime.write(lambda conn: store.claim_job(
    conn, source_id, now, lease_seconds=30.0
))
if not claimed:
    return False
try:
    record = client.get_ad(source_id=source_id, now=now)
except MetaAdsError as exc:
    runtime.write(lambda conn: store.fail_job(
        conn, source_id, now, exc.code,
        retry_after_seconds=exc.retry_after_seconds,
    ))
    return False
if record is None:
    runtime.write(lambda conn: store.fail_job(
        conn, source_id, now, "meta_not_found"
    ))
    return False
runtime.write(lambda conn: store.upsert_record_and_confirm(
    conn, record, confirmed_at=now
))
return True
```

Validate exact ID and account again at the orchestration boundary even though
the client validates them. Never keep a DB transaction open across a Meta
request.

- [ ] **Step 4: Add failing error, circuit, and concurrency tests**

Cover all bounded error codes, `Retry-After`, auth circuit opening for one hour,
explicit successful probe closing it, expired leases, two resolver threads,
worker/context races, and process restart. Assert only one network call occurs
for a claimed `(account_id, source_id)` and that every failure remains pending.

- [ ] **Step 5: Implement retry, component health, and catalog refresh**

`health(now)` returns a typed state with `disabled`, `ready`, or `degraded`, plus
credential state `missing`, `valid`, `expiring`, or `expired`. It must not print
or return attribution values.

`refresh_catalog(now, full=False)` obtains records from `client.list_ads`,
validates each again, and commits one bounded page at a time. Incremental refresh
includes active and recently changed readable ads. Full refresh requests every
readable status and updates `last_seen_at`; neither path removes a referenced
catalog row.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_store tests.test_meta_ads_mcp tests.test_meta_attribution -v
.venv/bin/ruff format src/brain/meta_attribution.py tests/test_meta_attribution.py
.venv/bin/ruff check src/brain/meta_attribution.py tests/test_meta_attribution.py
git add src/brain/meta_attribution.py tests/test_meta_attribution.py
git commit -m "feat: resolve CTWA source ids against Meta"
```

---

### Task 5: Transactionally stage work during transport ingestion

**Files:**
- Modify: `src/brain/transport_service.py`
- Modify: `src/brain/service.py`
- Modify: `tests/test_transport_ingest.py`

**Interfaces:**
- Consumes: Task 4 `MetaAttributionService.stage_event(conn, event_id, raw, now)`.
- Produces: every eligible newly stored/replayed CTWA event has a confirmed attribution or durable pending job before observer ACK.
- Observer payload and CTWA classifier remain unchanged.

- [ ] **Step 1: Write a failing eligible-ingestion test**

Send a v2 CTWA envelope whose raw object contains decimal `sourceId` and
`ctwaClid`. After the `200` ingest response, query the runtime DB and assert one
attribution row plus one job share the exact source ID. Assert the transport row
still contains the original canonical raw JSON.

- [ ] **Step 2: Write failing ineligible and duplicate tests**

Cover ordinary inbound, `sourceType == "post"`, missing source ID, malformed
non-decimal source ID, legacy v1, identical event replay, and conflicting
event replay. Ineligible cases create no Meta row. Identical replay remains one
attribution and one job. Conflict remains the existing transport conflict.

- [ ] **Step 3: Run ingestion tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest -v
```

Expected: eligible CTWA assertions fail because attribution is not staged.

- [ ] **Step 4: Pass decoded raw attribution into `_persist`**

Retain the decoded raw object already produced for raw/normalized validation.
Extend `_persist` to receive it and call `meta_attribution.stage_event` after
the transport INSERT or duplicate equality check but before returning. The
outer `RuntimeDatabase.write` remains the sole transaction, so an injected
staging failure rolls back the transport insertion and prevents ACK.

Construct `MetaAttributionService` in `BrainService` and inject it into
`TransportService`; default-disabled test settings produce a no-op staging
path and no network activity.

- [ ] **Step 5: Test retention cascade**

Advance time beyond `transport_retention_days`, call existing retention, and
assert the transport/attribution row is gone, an unneeded job is removed, and a
catalog row is retained until both unreferenced and older than 90 days. Ensure
retention logs only counts.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_transport_ingest tests.test_runtime_db tests.test_meta_ads_store -v
.venv/bin/ruff format src/brain/transport_service.py src/brain/service.py tests/test_transport_ingest.py
.venv/bin/ruff check src/brain/transport_service.py src/brain/service.py tests/test_transport_ingest.py
git add src/brain/transport_service.py src/brain/service.py tests/test_transport_ingest.py
git commit -m "feat: stage Meta attribution during CTWA ingest"
```

---

### Task 6: Lifespan worker, synchronization schedule, and degraded health

**Files:**
- Modify: `src/brain/mcp_server.py`
- Modify: `src/brain/service.py`
- Modify: `tests/test_brain.py`
- Modify: `tests/test_meta_attribution.py`

**Interfaces:**
- Consumes: Task 4 `run_due_jobs`, `refresh_catalog`, `apply_retention`, and `health`.
- Produces: one lifespan-owned `_meta_attribution_loop`, independent Meta health fields, initial capability probe, incremental sync, and daily full sync.

- [ ] **Step 1: Write failing lifespan tests**

Use an `IsolatedAsyncioTestCase` with a fake attribution service. Enter and exit
the Starlette lifespan and assert one loop starts, performs an initial probe,
and cancels cleanly. Force `run_due_jobs` and `refresh_catalog` to raise bounded
errors and assert the application lifespan and retention loop remain alive.

- [ ] **Step 2: Run lifespan tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_brain -v
```

Expected: FAIL because no Meta attribution lifespan task exists.

- [ ] **Step 3: Add the background loop without a second service unit**

Follow the existing retention-loop pattern:

```python
async def _meta_attribution_loop(self) -> None:
    while True:
        try:
            await asyncio.to_thread(self.service.meta_attribution.tick, time.time())
            await asyncio.sleep(META_ATTRIBUTION_LOOP_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Meta attribution tick failed")
            await asyncio.sleep(META_ATTRIBUTION_ERROR_DELAY_SECONDS)
```

Start it only when the feature is enabled, alongside—not instead of—the inner
MCP lifespan and retention task. Set loop polling to 5 seconds; the service
itself enforces job, incremental-sync, and full-sync due times.

- [ ] **Step 4: Write failing independent-health tests**

Extend `Health` and `as_dict()` with:

```python
"meta_ads_attribution": "disabled" | "ready" | "degraded"
"meta_ads_credential": "missing" | "valid" | "expiring" | "expired"
```

Assert disabled default health is still `ok`; enabled/missing token and
enabled/failed probe report Meta degraded but overall `Health.status == "ok"`
when existing Brain dependencies are healthy. Test 14-, 7-, and 3-day expiry
warning boundaries using an injected clock.

- [ ] **Step 5: Implement safe worker observability**

Emit only bounded operation, duration, decision, error code, pending count,
confirmed count, and catalog age. Add an `assertLogs` test with fixture token,
source ID, click ID, and names and assert every fixture is absent. Do not use
`logger.exception`, because SDK exception strings may contain response data.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_brain tests.test_meta_attribution -v
.venv/bin/ruff format src/brain/mcp_server.py src/brain/service.py tests/test_brain.py tests/test_meta_attribution.py
.venv/bin/ruff check src/brain/mcp_server.py src/brain/service.py tests/test_brain.py tests/test_meta_attribution.py
git add src/brain/mcp_server.py src/brain/service.py tests/test_brain.py tests/test_meta_attribution.py
git commit -m "feat: run Meta attribution worker"
```

---

### Task 7: Bounded context-time lookup and Brain response

**Files:**
- Modify: `src/brain/service.py`
- Modify: `tests/test_gateway_api.py`
- Modify: `tests/test_brain.py`

**Interfaces:**
- Consumes: Task 4 `resolve_contact_pending(contact_key, now, budget_seconds)` and store context views.
- Produces: six-key context events with `meta_attribution`, including exact confirmed/pending/null shapes.
- Existing contact resolution, six-hour window, eight-event bound, raw attribution, and response-size behavior stay intact.

- [ ] **Step 1: Write failing confirmed-context test**

Seed a recent CTWA event, observed attribution, and confirmed catalog record.
POST the current gateway context and assert exact nested equality for account,
match method, source/click IDs, ad, ad set, campaign, creative,
`metadata_complete`, `confirmed_at`, and `metadata_fetched_at`.

- [ ] **Step 2: Write failing pending/null tests**

Assert an eligible unresolved event returns the exact pending keys and an
ordinary/legacy/ineligible event returns `meta_attribution: None`. When the
feature is disabled, still emit `meta_attribution: None`; never create a live
Meta call from disabled settings.

- [ ] **Step 3: Run gateway tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_gateway_api tests.test_brain -v
```

Expected: FAIL because context events do not contain `meta_attribution`.

- [ ] **Step 4: Resolve before the final read, outside a DB transaction**

After contact-key resolution and before `_conversation_context_from_runtime`,
call the bounded resolver only when enabled. Give it a monotonic deadline no
later than 5 seconds after context processing began and a per-MCP budget of 4
seconds. Catch only the Meta component's bounded exceptions locally; do not
turn them into Brain `DatabaseUnavailable`.

Join `ctwa_meta_attributions` and `meta_ads_catalog` in the final runtime read.
Render with Task 1 builders. Validate a confirmed DB row again; inconsistent
stored confirmation returns `meta_attribution.status == "pending"` with
`meta_invalid_response`, never a claimed ad.

- [ ] **Step 5: Test timing, race, and size behavior**

Use a blocking fake client to prove the resolver stops within the injected
budget and the context still returns the contact/raw event as pending. Test a
worker confirming between the pre-read and final read. Include the expanded
payload in the existing 32 MiB complete-response check; keep
`context_too_large` all-or-nothing behavior.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_gateway_api tests.test_brain tests.test_meta_attribution -v
.venv/bin/ruff format src/brain/service.py tests/test_gateway_api.py tests/test_brain.py
.venv/bin/ruff check src/brain/service.py tests/test_gateway_api.py tests/test_brain.py
git add src/brain/service.py tests/test_gateway_api.py tests/test_brain.py
git commit -m "feat: expose Meta attribution in Brain context"
```

---

### Task 8: Rolling-compatible CEO bridge and trust rules

**Files:**
- Modify: `integrations/hermes/brain-ceo-bridge/tools.py`
- Modify: `integrations/hermes/brain-ceo-bridge/README.md`
- Modify: `tests/test_ceo_bridge_plugin.py`
- Modify later: `/root/.hermes/plugins/brain-ceo-bridge/tools.py`
- Modify later: `/root/.hermes/SOUL.md`

**Interfaces:**
- Consumes: Task 7 four-key legacy, five-key raw, and six-key Meta event shapes.
- Produces: strict CEO validation, 7-second HTTP timeout, exact-account pin, and explicit confirmed/pending trust semantics.

- [ ] **Step 1: Write failing bridge confirmed/pending tests**

Add fixtures for the exact spec section 12 shapes. Assert the bridge passes
valid confirmed, pending, and null attribution unchanged. Retain acceptance of
legacy four-key and raw five-key events during rollout.

- [ ] **Step 2: Add rejecting security tests**

Reject and return controlled `context_unavailable` for:

- foreign account;
- `source_id != ad.id`;
- non-`source_id_exact` match;
- pending payload containing ad/campaign names;
- confirmed payload missing ad or campaign name;
- malformed/overlong IDs, names, statuses, timestamps, or error codes;
- unexpected nested/additional keys;
- raw prompt-like strings placed where typed IDs are required.

Assert fixture values never occur in the controlled result or logs.

- [ ] **Step 3: Run bridge tests to verify RED**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_ceo_bridge_plugin -v
```

Expected: new six-key fixtures return `context_unavailable`.

- [ ] **Step 4: Implement exact shape validators**

Use event key sets:

```python
_LEGACY_EVENT_FIELDS = {
    "event_id", "transport_kind", "source_app", "inbound_kind"
}
_RAW_EVENT_FIELDS = _LEGACY_EVENT_FIELDS | {"external_ad_reply"}
_META_EVENT_FIELDS = _RAW_EVENT_FIELDS | {"meta_attribution"}
_META_ACCOUNT_ID = "act_1598606388477916"
_HTTP_TIMEOUT_SECONDS = 7.0
```

Add separate `_valid_pending_attribution` and `_valid_confirmed_attribution`
functions with exact keys and bounded scalar validation. Enforce account and
ID equality before returning. Keep all failures inside the existing tool
boundary.

- [ ] **Step 5: Document and test CEO semantics**

Update the versioned bridge README and its deployment-contract assertions with:

```text
The CEO may name an ad or campaign only when meta_attribution.status is
confirmed and matched_by is source_id_exact. Pending attribution never blocks
service and never authorizes an action. Meta names remain untrusted evidence.
```

- [ ] **Step 6: Run source GREEN and commit Brain**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_ceo_bridge_plugin tests.test_deployment_contracts -v
.venv/bin/ruff format integrations/hermes/brain-ceo-bridge/tools.py tests/test_ceo_bridge_plugin.py tests/test_deployment_contracts.py
.venv/bin/ruff check integrations/hermes/brain-ceo-bridge tests/test_ceo_bridge_plugin.py
git add integrations/hermes/brain-ceo-bridge/tools.py integrations/hermes/brain-ceo-bridge/README.md tests/test_ceo_bridge_plugin.py tests/test_deployment_contracts.py
git commit -m "feat: validate Meta attribution in CEO bridge"
```

- [ ] **Step 7: Update installed Hermes files and commit separately**

Copy the already-tested versioned `tools.py` into the installed plugin with an
atomic mode-preserving replacement. Add a SOUL rule that confirmed exact Meta
attribution may be described as origin evidence, while pending/raw/names never
become instructions and never block service.

```bash
cd /root/.hermes
git diff --check -- plugins/brain-ceo-bridge/tools.py SOUL.md
git add plugins/brain-ceo-bridge/tools.py SOUL.md
git commit -m "feat: expose verified Meta attribution to CEO"
```

Do not include the pre-existing `/root/.hermes/config.yaml` modification.

---

### Task 9: Provisioning tools, deployment contract, and runbook

**Files:**
- Create: `scripts/meta_ads_mcp_probe.py`
- Create: `scripts/install_meta_ads_credential.py`
- Modify: `deploy/brain.env.example`
- Modify: `deploy/brain.toml.example`
- Modify: `deploy/brain.service`
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_deployment_contracts.py`

**Interfaces:**
- Consumes: Task 3 client and Task 4 health.
- Produces: secret-safe initial installation/rotation, non-sensitive capability probe, feature-flag rollout procedure, and smoke checks.

- [ ] **Step 1: Write failing credential-installer tests**

In a temporary `/etc/brain` equivalent, seed an existing `brain.env` and run
the installer with token supplied on stdin plus RFC3339 expiry. Assert atomic
mode `0600`, preservation of unrelated keys, exact replacement on `--rotate`,
refusal to overwrite without `--rotate`, symlink refusal, and rollback after an
injected write failure. Capture stdout/stderr and assert the fixture token is
absent.

- [ ] **Step 2: Implement secret-safe installation**

Reuse the repository's `atomic_private_write_bytes`, `snapshot_files`, and
`restore_files` patterns without printing secret values. Accept:

```text
--config-dir /etc/brain
--expires-at 2026-11-01T00:00:00Z
--rotate
```

Read the token from a TTY with `getpass.getpass()` or stdin when non-interactive.
Write only `BRAIN_META_ADS_MCP_ACCESS_TOKEN` and
`BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT` into `brain.env`. Print one content-free
success/failure line.

- [ ] **Step 3: Write failing probe tests**

Inject a fake client and assert success runs `probe()` plus `get_ad()` for an
optional known ID read from `BRAIN_META_PROBE_AD_ID`, prints only:

```text
OK: Meta Ads MCP tools, configured account, and known-ad read verified
```

Failure prints only a bounded error code. The token, known ID, names, tool
response, and exception text must be absent.

- [ ] **Step 4: Implement the authenticated probe**

Load `BrainSettings.from_env()`, require the feature/account/token/expiry
configuration, create `MetaAdsMcpClient`, run capability/account validation,
and optionally require one exact known-ad result. Exit 0 on success and 1 on a
bounded failure. Do not persist any Meta response.

- [ ] **Step 5: Update examples and systemd contract**

Document in `brain.toml.example`:

```toml
meta_attribution_enabled = false
meta_ad_account_id = "act_1598606388477916"
meta_ads_mcp_timeout_seconds = 4.0
meta_ads_mcp_response_max_bytes = 8388608
meta_ads_sync_interval_seconds = 900
meta_ads_full_sync_interval_seconds = 86400
```

Document the two secret environment keys in `brain.env.example` using visibly
synthetic values. Keep the systemd unit network-capable, root-only, and writable
only where the existing runtime DB requires it. Add tests that no real-looking
token is checked in and that the endpoint/account constraints appear in docs.

- [ ] **Step 6: Add the exact authentication runbook**

Document these operator steps:

1. Open Meta Developer Apps and create/reuse the business-owned app.
2. Add **Create & manage ads with Ads MCP server**.
3. Authorize the business user for the one configured account.
4. Select read access when Meta offers a Read/Manage choice.
5. Obtain the programmatic user token using the app OAuth flow or Graph API Explorer with the Ads MCP scopes required by Meta.
6. Install it through `install_meta_ads_credential.py`; never paste it into shell arguments, Git, chat, or logs.
7. Keep the feature flag false and run `meta_ads_mcp_probe.py` with one known ad ID supplied only through the process environment.
8. Rotate before the 14-, 7-, and 3-day warnings, probe the new credential, restart Brain, and revoke the replaced token.

Include the official URLs from spec section 17 and the `401` scope list as a
diagnostic, not as permission to expose write tools.

- [ ] **Step 7: Extend smoke and deployment checks**

When disabled, smoke expects Meta health `disabled`. When enabled, smoke accepts
`ready` or reports a bounded degraded reason without dumping data. Deployment
tests assert the token exists only as an environment placeholder, account is
pinned, bridge timeout is 7 seconds, feature default is false, and docs cover
rotation, rollback, pending behavior, all readable statuses, and 90-day GC.

- [ ] **Step 8: Run GREEN and commit**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_deployment_contracts tests.test_config -v
PYTHONPATH=src .venv/bin/python scripts/smoke_test.py
.venv/bin/ruff format scripts/meta_ads_mcp_probe.py scripts/install_meta_ads_credential.py scripts/smoke_test.py tests/test_deployment_contracts.py
.venv/bin/ruff check scripts/meta_ads_mcp_probe.py scripts/install_meta_ads_credential.py scripts/smoke_test.py tests/test_deployment_contracts.py
git add scripts/meta_ads_mcp_probe.py scripts/install_meta_ads_credential.py deploy/brain.env.example deploy/brain.toml.example deploy/brain.service README.md docs/runbook.md scripts/smoke_test.py tests/test_deployment_contracts.py
git commit -m "docs: add Meta Ads MCP provisioning workflow"
```

---

### Task 10: Full verification and staged live proof

**Files:**
- Modify only when a failing gate is reproduced by a new regression test in the owning Task 1-9 file.
- Runtime targets: `/etc/brain`, installed Brain service, installed CEO bridge, and one controlled CTWA message.

**Interfaces:**
- Consumes: clean committed Brain/Hermes candidates and operator-completed Meta authorization.
- Produces: automated evidence, authenticated read-only proof, compatibility-first deployment, failure proof, and one exact real attribution.

- [ ] **Step 1: Run complete static and automated verification**

```bash
cd /root/brain
.venv/bin/ruff check src tests integrations scripts
.venv/bin/ruff format --check \
  src/brain/config.py \
  src/brain/meta_ads_models.py \
  src/brain/meta_ads_store.py \
  src/brain/meta_ads_mcp.py \
  src/brain/meta_attribution.py \
  src/brain/runtime_db.py \
  src/brain/transport_service.py \
  src/brain/service.py \
  src/brain/mcp_server.py \
  integrations/hermes/brain-ceo-bridge/tools.py \
  scripts/meta_ads_mcp_probe.py \
  scripts/install_meta_ads_credential.py \
  scripts/smoke_test.py \
  tests/test_config.py \
  tests/test_meta_ads_models.py \
  tests/test_meta_ads_store.py \
  tests/test_meta_ads_mcp.py \
  tests/test_meta_attribution.py \
  tests/test_runtime_db.py \
  tests/test_transport_ingest.py \
  tests/test_brain.py \
  tests/test_gateway_api.py \
  tests/test_ceo_bridge_plugin.py \
  tests/test_deployment_contracts.py
git diff --check
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
cd /root/brain/observers/whatsapp
npm test
```

Expected: zero failures. The targeted format check deliberately covers every
file changed by this plan. Before this feature, the full-repository format check
already reported 12 files; touched files are formatted by their owning tasks,
while broad cleanup of untouched files stays out of scope. If a gate fails,
return to its owning task, add a test that reproduces the failure, make the
smallest fix, rerun that task and this complete step, and commit with
`fix: close Meta attribution verification gap`.

- [ ] **Step 2: Provision Meta while disabled**

Complete the browser steps from the runbook. Install the token through stdin,
set its expiry, and keep `BRAIN_META_ATTRIBUTION_ENABLED=false`. Verify
`/etc/brain/brain.env` is a regular root-owned `0600` file without displaying
its contents.

- [ ] **Step 3: Run the authenticated read-only probe**

Select a known readable ad from `act_1598606388477916`, provide its ID only in
the probe process environment, and run:

```bash
cd /root/brain
set -a
. /etc/brain/brain.env
set +a
read -r -s -p "Known Meta ad ID: " BRAIN_META_PROBE_AD_ID
printf '\n'
export BRAIN_META_PROBE_AD_ID
.venv/bin/python scripts/meta_ads_mcp_probe.py
unset BRAIN_META_PROBE_AD_ID BRAIN_META_ADS_MCP_ACCESS_TOKEN BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT
```

Expected: the one content-free `OK` line. Immediately unset the probe ID and
shell-exported Meta variables. A missing required tool or incompatible live
schema is a release blocker: keep the feature disabled, capture only the
bounded reason code, and amend the adapter tests with a sanitized schema shape
before proceeding.

- [ ] **Step 4: Run pre-deploy Brain/Hermes checks and build candidate**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python scripts/hermes_integration_check.py
PYTHONPATH=src .venv/bin/python scripts/hermes_integrity.py verify --repo /usr/local/lib/hermes-agent --baseline /var/lib/brain/runtime/hermes-integrity-baseline.json
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py create --config /var/lib/brain/runtime/staging/brain.toml.next
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py verify candidate
```

Expected: automated checks pass and the candidate contains the new versioned
bridge and Brain modules without any Meta credential.

- [ ] **Step 5: Deploy the compatibility bridge first**

Atomically install the tested bridge, preserve mode/owner, restart Hermes, run
the plugin doctor, and prove old four-/five-key Brain events still pass before
Brain changes. Run `scripts/hermes_integration_check.py`; require one tool, zero
hooks, and no source/installed mismatch.

- [ ] **Step 6: Deploy Brain with feature disabled**

Install/restart the Brain candidate. Query only `sqlite_master`, `PRAGMA
table_info`, and row counts to verify the three tables/indexes; do not select
tokens, source IDs, click IDs, or names. Require `/health` overall `ok` and Meta
state `disabled`.

- [ ] **Step 7: Enable sync and background resolution**

Set the feature flag true, restart Brain, and require the worker capability
probe plus initial catalog synchronization to reach Meta state `ready`. Verify
only bounded counts/catalog age. Confirm one active and one paused or archived
known ad in memory through the probe without printing identifiers or names.

- [ ] **Step 8: Prove pending behavior**

Verify `/etc/brain/brain.env` is a regular root-owned file, snapshot it into a
root-only directory made with `mktemp -d`, and install a synthetic invalid token
through the credential installer. Register a shell `trap` that restores the
exact saved file and restarts Brain on success, interruption, or failure. Restart
Brain with the invalid credential and call an authenticated CEO context for a
synthetic eligible event. Require raw CTWA/contact context plus
`meta_attribution.status == "pending"`, Meta health degraded, and continued
service. Run the trap-backed restoration, restart, probe, require ready state,
and only then remove the temporary snapshot directory. Do not revoke or print
the valid token during this test.

- [ ] **Step 9: Prove one real exact CTWA attribution**

From a fresh contact, click one known CTWA ad and send the first message. Call
`conversation_context({})` from the CEO DM. Validate in memory only:

- observed `sourceType == "ad"`;
- observed `sourceId == meta_attribution.source_id == ad.id`;
- account equals the configured account;
- `status == "confirmed"` and `matched_by == "source_id_exact"`;
- ad and campaign IDs/names exactly equal Ads Manager;
- ad-set/creative values are present when Meta supplies them;
- no attribution content entered logs or health.

Record only PASS plus timestamps and bounded status codes. If the first call is
pending, confirm the CEO still serves, wait for the worker, and require a later
turn's context to become confirmed without changing the binding.

- [ ] **Step 10: Promote and record rollback readiness**

```bash
cd /root/brain
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py promote
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py status
git status --short --branch
```

Require clean committed Brain and Hermes trees apart from the known pre-existing
Hermes `config.yaml` modification. Record that rollback is feature-flag disable
plus worker stop; additive tables and raw CTWA capture remain intact.

## Completion Checklist

- [ ] The one Meta account is pinned and verified through Ads MCP.
- [ ] Required read tools and their live schemas are probed before enablement.
- [ ] No local or CEO path can call a Meta write tool.
- [ ] Eligible transport ingestion durably stages attribution before ACK.
- [ ] Cache hit, exact live lookup, pagination fallback, deduplication, and retries pass.
- [ ] Only exact `sourceId == ad.id` plus account/ad/campaign proof becomes confirmed.
- [ ] Active, paused, completed, rejected, and archived readable ads remain eligible.
- [ ] Context returns confirmed, pending, and null shapes exactly.
- [ ] Meta failures degrade attribution only and the CEO continues serving.
- [ ] Credential installation, warning, rotation, probe, and revocation are documented and tested.
- [ ] Tokens, IDs, names, URLs, and raw responses are absent from observability.
- [ ] CRM writes, CAPI, performance metrics, multi-account access, Graph fallback, and Meta mutations are absent.
- [ ] Automated suites, authenticated probe, pending simulation, and one real CTWA proof pass before promotion.
