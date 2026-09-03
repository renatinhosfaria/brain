# Remote Meta Ads MCP Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Brain to the existing remote `mcp-meta-ads` Streamable HTTP
endpoint, resolve each eligible CTWA `sourceId` to the exact active Meta ad
and campaign, persist only validated attribution, and expose the confirmed
result through `conversation_context({})` without changing the remote MCP
repository or interrupting CEO atendimento.

**Architecture:** Add a read-only remote MCP client behind a Brain-owned
attribution service. Transport ingestion stages an idempotent attribution row
and a deduplicated source lookup job in the same SQLite transaction as the
CTWA event. A leased worker and the context request resolve pending jobs. The
client uses one lazily-created Streamable HTTP session per Brain process, a
strict tool allowlist, exact account probing, bounded response/latency budgets,
and no payload logging. The service writes immutable confirmed snapshots and
projects them into the existing bounded CEO context; all remote failures are
fail-open and represented by bounded state.

**Tech Stack:** Python 3.11+, `mcp==2.0.0`, `httpx2`, `anyio`, SQLite WAL,
`unittest`, Ruff, existing Starlette/uvicorn MCP server.

**Spec:** `docs/superpowers/specs/2026-09-02-ctwa-remote-meta-mcp-design.md`

## Global constraints

- Do not edit, fork, deploy, or add a dependency to
  `mcp-meta-ads`; this change is only in `/root/brain`.
- Do not use Meta's hosted MCP, OAuth, Graph API, or any alternate credential
  source. The remote server owns the Meta access token; Brain sends only its
  configured service API key.
- Brain may invoke only `meta_list_ad_accounts`, `meta_get_ad`, and
  `meta_get_campaign`. Presence of any other remote tool never authorizes a
  call.
- The configured account is exactly `act_1598606388477916`. The probe must
  observe that account and no other accessible account before any attribution
  lookup is allowed.
- The original decimal `sourceId` is the only ad selector. IDs, names, and
  statuses from the remote result are validated before persistence; no fuzzy,
  chronological, campaign-name, or inferred match is permitted.
- A confirmed row is immutable. A current `ACTIVE` status is recorded as the
  observed state and is not described as proof that the ad was active at the
  historical click time.
- The API key is read only from the service environment, never stored in
  SQLite/context, logged, included in process arguments, or copied into an
  exception. Remote response text and raw tool payloads are also excluded
  from logs and CEO context.
- Remote work is bounded by the remaining request budget and never blocks
  transport acknowledgement, context availability, or CEO atendimento.
- Preserve current raw CTWA capture, ordinary-event behavior, existing
  authorization, and all retention/security invariants.
- Every implementation task starts with a failing test, then the smallest
  implementation, then focused tests and a commit. Run the complete Python
  suite and Observer suite before declaring completion.

## 1. Configuration and dependency boundary

**Files:**

- Modify `pyproject.toml` and `uv.lock` to declare the direct `httpx2`
  dependency already used by the installed MCP SDK, keeping the lockfile
  reproducible.
- Modify `src/brain/config.py`.
- Modify `deploy/brain.env.example`, `deploy/brain.toml.example`, and
  `deploy/brain.service`.
- Extend `tests/test_config.py` and `tests/test_deployment_contracts.py`.

**Settings interface:** Add these frozen `BrainSettings` fields with the
following defaults and validation:

```python
meta_ads_mcp_enabled: bool = False
meta_ads_mcp_url: str = "https://mcp-facebook-ads.famachat.com.br/mcp"
meta_ads_mcp_api_key: str = ""                 # environment-only secret
meta_ad_account_id: str = "act_1598606388477916"
meta_ads_mcp_timeout_seconds: float = 4.0
meta_ads_mcp_response_max_bytes: int = 8 * 1024 * 1024
meta_ads_mcp_context_budget_seconds: float = 1.5
meta_ads_mcp_worker_interval_seconds: float = 15.0
```

`from_env()` reads the corresponding `BRAIN_META_ADS_*` variables (and the
`[server]` TOML values for non-secret fields), parses booleans strictly, and
never reads a file containing the API key. URL validation requires HTTPS,
host `mcp-facebook-ads.famachat.com.br`, path `/mcp`, no username/password,
query, fragment, or non-default port. Account validation accepts only the
canonical configured value and stores it in `act_...` form. Timeout, byte,
context-budget, and worker-interval values are finite positive bounded
numbers. Enabling with an empty key raises the safe configuration error
`meta_auth_unavailable` during service construction/probe, without echoing the
missing value.

Tests first assert strict URL/boolean/account parsing, secret non-persistence,
disabled defaults, and rejection of alternate hosts, redirects, userinfo,
query credentials, zero/negative limits, and extra accessible accounts. The
deployment contract asserts the example is disabled by default, the key is
commented as a root-only service secret, and the systemd unit reads only the
Brain environment file with `UMask=0077`.

Commit: `config: add remote Meta Ads MCP settings`.

## 2. Validated remote/domain models

**Files:**

- Create `src/brain/meta_ads_models.py`.
- Create `tests/test_meta_ads_models.py`.

Define the exact public model boundary used by the client, store, and service:

```python
META_READ_TOOLS = frozenset({
    "meta_list_ad_accounts",
    "meta_get_ad",
    "meta_get_campaign",
})
META_ERROR_CODES = frozenset({
    "meta_timeout", "meta_rate_limited", "meta_server_unavailable",
    "meta_auth_unavailable", "meta_required_tool_unavailable",
    "meta_account_mismatch", "meta_not_found", "meta_invalid_response",
    "meta_incomplete_result", "meta_inactive",
})

@dataclass(frozen=True)
class ObservedCtwaSource:
    source_id: str
    ctwa_clid: str | None

@dataclass(frozen=True)
class RemoteAd:
    ad_id: str
    name: str
    campaign_id: str
    status: str
    effective_status: str

@dataclass(frozen=True)
class RemoteCampaign:
    campaign_id: str
    name: str
    status: str
    effective_status: str

@dataclass(frozen=True)
class ConfirmedMetaAttribution:
    ad_id: str
    ad_name: str
    campaign_id: str
    campaign_name: str
    ad_status: str
    ad_effective_status: str
    campaign_status: str
    campaign_effective_status: str

class MetaAdsError(Exception):
    code: str
    retry_after_seconds: float | None
```

IDs are 1–64 ASCII decimal digits; the configured account is normalized only
between `act_1598606388477916` and its numeric form. Names/statuses are
non-empty strings capped at 512 UTF-8 bytes and free of control characters.
`observed_ctwa_source(raw)` accepts the canonical raw Observer object using
`sourceType`, `sourceId`, and optional `ctwaClid`; it returns `None` unless the
source is an ad with a valid original source ID. It never consults a hash or
reconstructs a value. Validators reject missing/mismatched IDs, null names,
non-`ACTIVE` statuses for confirmation, and unknown error codes.

Tests cover valid and invalid raw shapes, leading-zero/overlong IDs, exact
string equality, UTF-8/control-character limits, active-only confirmation,
and safe `MetaAdsError` stringification (code only).

Commit: `feat: define remote Meta attribution models`.

## 3. Streamable HTTP client with a strict read-only allowlist

**Files:**

- Create `src/brain/meta_ads_mcp.py`.
- Create `tests/test_meta_ads_mcp.py`.

Implement `RemoteMetaAdsMcpClient` with this synchronous service-facing API;
the async implementation runs on one private event-loop thread so the
existing synchronous Brain service can reuse one MCP session safely:

```python
class RemoteMetaAdsMcpClient:
    def __init__(self, settings: BrainSettings) -> None: ...
    def probe(self, deadline: float | None = None) -> None: ...
    def get_ad(self, source_id: str, deadline: float | None = None) -> RemoteAd: ...
    def get_campaign(self, campaign_id: str,
                     deadline: float | None = None) -> RemoteCampaign: ...
    def invalidate(self) -> None: ...
    def close(self) -> None: ...
```

The loop thread lazily creates at most one `httpx2.AsyncClient`,
`streamable_http_client`, and `mcp.ClientSession`. An `asyncio.Lock` covers
initialize/tool calls; transport/protocol/auth failures close and recreate the
session. The HTTP client sets the bearer header, the configured timeout,
`follow_redirects=False`, `trust_env=False`, and the exact configured URL.
Use a counting async byte stream/shared operation budget so a response above
`meta_ads_mcp_response_max_bytes` becomes `meta_invalid_response` before its
payload can be retained. No response body is logged.

The first operation calls `initialize`, `list_tools`, and validates that all
three read tools are present. It then calls `meta_list_ad_accounts` with the
remote server's existing schema and requires exactly one account matching
`act_1598606388477916`; missing, extra, or differently formatted accounts map
to `meta_account_mismatch`. `get_ad` calls only `meta_get_ad` with
`{"ad_id": source_id}`; `get_campaign` calls only `meta_get_campaign` with
`{"campaign_id": campaign_id}`. Structured MCP results are parsed without
trusting text content, and a remote tool error is mapped to the bounded error
vocabulary. A 401/403 opens the service circuit; no retry loop or alternate
credential is attempted.

Tests use an injected fake session/transport (never a live API key) to cover
header construction, exact URL and no redirects, initialize/session reuse,
recreation after failure, timeout and response-size limits, required-tool
validation, account probe success/missing/extra/mismatch, structured ad and
campaign parsing, malformed/error results, 401/403 mapping, and proof that
write-tool names are never called.

Commit: `feat: add read-only remote Meta Ads MCP client`.

## 4. Durable attribution schema and store

**Files:**

- Modify `src/brain/runtime_db.py`.
- Create `src/brain/meta_ads_store.py`.
- Create `tests/test_meta_ads_store.py` and extend `tests/test_runtime_db.py`.

Extend the runtime schema with foreign keys and indexes:

```sql
CREATE TABLE IF NOT EXISTS ctwa_meta_attributions (
  event_id TEXT PRIMARY KEY REFERENCES transport_events(event_id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  ctwa_clid TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','confirmed','unavailable')),
  ad_id TEXT, ad_name TEXT, campaign_id TEXT, campaign_name TEXT,
  ad_status TEXT, ad_effective_status TEXT,
  campaign_status TEXT, campaign_effective_status TEXT,
  match_method TEXT CHECK(match_method IS NULL OR match_method='source_id_exact'),
  reason_code TEXT, confirmed_at REAL, last_attempt_at REAL,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  next_attempt_at REAL, lease_until REAL, lease_token TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_attribution_jobs (
  account_id TEXT NOT NULL, source_id TEXT NOT NULL,
  next_attempt_at REAL NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT, lease_until REAL, lease_token TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(account_id, source_id)
);
CREATE TABLE IF NOT EXISTS meta_attribution_state (
  account_id TEXT PRIMARY KEY, auth_circuit_until REAL NOT NULL DEFAULT 0,
  last_probe_at REAL, last_success_at REAL, updated_at REAL NOT NULL
);
```

Add indexes for `(status, next_attempt_at)`, `(source_id, status)`, and
`transport_events(contact_key, received_at)`. Existing databases are migrated
inside the current `BEGIN IMMEDIATE` initialization path; migration is
idempotent and rejects incompatible columns instead of silently weakening a
constraint.

`MetaAdsStore` exposes transactional methods with explicit behavior:

```python
stage_event(conn, event_id, observed, now) -> None
claim_source_job(conn, source_id, now, lease_seconds) -> str | None
complete_source(conn, source_id, confirmed, now, lease_token) -> int
fail_source(conn, source_id, reason_code, now, lease_token) -> bool
due_source_ids(conn, now, limit) -> list[str]
open_auth_circuit(conn, now, retry_after) -> float
close_auth_circuit(conn, now) -> None
context_for_event(conn, event_id) -> dict[str, object] | None
purge_expired(conn, now) -> int
```

Staging is idempotent on `event_id` and rejects a replay whose original source
ID/CLID differs. It creates one job per source ID; a confirmed source updates
all of its pending event rows in one short transaction, while existing
confirmed snapshots are never overwritten. Lease claims use a random token,
expiry, and SQLite row-count checks so two workers cannot resolve the same
source concurrently. Retry delays are bounded (`60s, 5m, 15m, 1h, 6h, 24h`)
with no tight auth retry. Deleting a parent transport event cascades its
attribution row; orphan jobs are removed by retention. No remote values are
stored until exact validation is complete.

Tests cover fresh schema/migration, foreign-key cascade, idempotent replay and
conflict, pending→confirmed/unavailable transitions, source deduplication,
lease races/expiry, retry schedule, auth circuit, immutable confirmation,
bounded reason codes, and retention cleanup.

Commit: `feat: persist durable CTWA Meta attribution jobs`.

## 5. Attribution orchestration and exact confirmation

**Files:**

- Create `src/brain/meta_attribution.py`.
- Create `tests/test_meta_attribution.py`.
- Modify `src/brain/transport_service.py` and
  `tests/test_transport_ingest.py`.

Define `MetaAttributionService`:

```python
class MetaAttributionService:
    def stage_event(self, conn, *, event_id: str, raw: object, now: float) -> None: ...
    def resolve_source(self, source_id: str, now: float,
                       budget_seconds: float | None = None) -> bool: ...
    def resolve_pending_for_contact(self, event_ids: list[str], now: float,
                                    budget_seconds: float) -> int: ...
    def run_due_jobs(self, now: float, limit: int = 20) -> int: ...
    def probe(self, now: float) -> str: ...
    def tick(self, now: float) -> int: ...
    def health(self, now: float) -> str: ...
```

In `TransportService.ingest`, after validating/canonicalizing the raw CTWA and
before committing the existing transaction, call `stage_event` with the
decoded original raw object. A malformed/non-ad source remains ordinary
transport evidence under the existing rules; a valid CTWA source gets a
pending row even when the feature is disabled only if the feature was enabled
for that ingestion (otherwise no remote job is created). ACK behavior and raw
capture remain unchanged.

`resolve_source` first checks the enabled flag and durable auth circuit, claims
the source job, then performs this exact sequence within the remaining deadline:

1. Ensure a successful probe for the configured account and three read tools.
2. Call `get_ad(source_id)` and require `ad.ad_id == source_id` byte-for-byte,
   non-empty name/campaign ID, valid ID/status, and
   `ad.effective_status == 'ACTIVE'`.
3. Call `get_campaign(ad.campaign_id)` and require exact campaign ID,
   non-empty name, valid status, and `effective_status == 'ACTIVE'`.
4. Persist `ConfirmedMetaAttribution` with `match_method='source_id_exact'`
   only while holding the lease.

Map inactive/missing/malformed/exactness failures to pending or unavailable
with bounded reason codes; map 401/403 to the durable account circuit. Never
persist an ad/campaign name from a failed or incomplete comparison. Use a
monotonic deadline so both remote calls share the context budget; the worker
uses the normal configured timeout.

Tests exercise staging in the ingestion transaction, all success predicates,
each exact-ID/name/status failure, account/probe failures, timeout and circuit
behavior, idempotent confirmation, worker retry, and fail-open ACK guarantees.

Commit: `feat: resolve CTWA sources through remote Meta MCP`.

## 6. Brain lifecycle, context projection, and health

**Files:**

- Modify `src/brain/service.py`, `src/brain/mcp_server.py`, and
  `tests/test_brain.py`.
- Create/extend `tests/test_meta_context_integration.py`.

Construct the client/store/service only after runtime schema initialization;
keep `BrainService` startup free of remote network calls. Add the attribution
component to `Health` as `meta_ads_mcp` with values `disabled`, `ready`, or
`degraded`; a degraded remote dependency does not change the core Brain status
from healthy to unavailable when SQLite, identity, and bridge checks pass.

Extend `_conversation_context_from_runtime` to left-join
`ctwa_meta_attributions` by `event_id`. Preserve all current contact, event,
raw `external_ad_reply`, and `inbound_kind` fields. Add `meta_attribution` only
for a CTWA event with a stored row:

- `confirmed`: `status`, `ad_id`, `ad_name`, `campaign_id`, `campaign_name`;
- `pending`/`unavailable`: `status` and a bounded `reason` only;
- ordinary events: no Meta field.

Before the final context read, `gateway_conversation_context` may ask the
attribution service to resolve at most the newest pending CTWA source for this
contact, using `min(1.5s, remaining request deadline)` and never extending the
existing HTTP deadline. If it cannot resolve in time, return the normal raw
context and pending/unavailable state. This preserves the CEO's continued
atendimento while allowing a newly arrived lead to become confirmed during
the first `conversation_context({})` call.

Add a lifespan task in `BrainMCPServer` that calls `tick` every
`meta_ads_mcp_worker_interval_seconds` in `asyncio.to_thread`, catches and
contains housekeeping exceptions, and cancels cleanly. The task does not run
in `BrainService.__init__`, and `close()` is called during lifespan shutdown.
The worker probes lazily, processes due jobs in a bounded batch, and respects
the auth circuit. Context and worker calls share the same client/session lock.

Tests cover disabled/no-network startup, health states, context projection for
confirmed/pending/unavailable/ordinary events, newest-event on-demand
resolution, request-budget fail-open, worker lifecycle/cancellation, and no
secret/raw-remote-payload leakage in context or logs.

Commit: `feat: expose Meta attribution through Brain context`.

## 7. CEO bridge contract

**Files:**

- Modify `integrations/hermes/brain-ceo-bridge/tools.py`.
- Modify `integrations/hermes/brain-ceo-bridge/README.md`.
- Extend `tests/test_ceo_bridge_plugin.py`.

Extend the exact event allowlist with `meta_attribution` and add a strict
validator. Confirmed objects must contain exactly the five public fields
`status`, `ad_id`, `ad_name`, `campaign_id`, and `campaign_name`, with decimal
IDs and bounded safe names. Pending/unavailable objects must contain only
`status` and a safe bounded reason. The validator accepts the field only when
`transport_kind == 'ctwa_candidate'`, rejects unknown keys, inactive or
malformed values, unconfirmed names, attribution on ordinary events, and any
token/remote-payload-shaped data. Keep the existing session, phone, raw-value,
response-size, and fail-closed validation unchanged.

Document that `conversation_context({})` can contain exact confirmed ad and
campaign names, that current `ACTIVE` is not historical click-time proof, and
that pending/unavailable means the CEO continues without asking the contact to
identify its own phone or ad. Add fixtures for valid confirmation and every
rejection path.

Commit: `feat: validate Meta attribution in CEO bridge`.

## 8. Probe command, deployment, and operator runbook

**Files:**

- Create `scripts/meta_ads_mcp_probe.py`.
- Modify `deploy/brain.env.example`, `deploy/brain.toml.example`,
  `deploy/brain.service`, `README.md`, and `docs/runbook.md`.
- Extend `tests/test_deployment_contracts.py` and add
  `tests/test_meta_ads_probe.py`.

The probe command loads the same `BrainSettings`, constructs the client, and
prints only one of `disabled`, `ready account=act_1598606388477916`, or a
bounded error code. It exits `0` only for a successful enabled probe and never
accepts an API key or URL argument. It must not print the key, response body,
source ID, ad name, or campaign name. The systemd example keeps the feature
disabled until this probe passes, supplies the key from a root-only
`/etc/brain/brain.env` (mode `0600`), preserves `UMask=0077`, and grants no
additional network or filesystem path beyond the existing Brain service.

Document the exact rollout:

1. Restrict the remote MCP's Meta token to `act_1598606388477916` and verify
   its HTTPS certificate/host.
2. Install the Brain API key in the root-only service secret environment.
3. Run `python scripts/meta_ads_mcp_probe.py` while the feature is disabled,
   then enable it and run the probe again.
4. Restart Brain, verify health `ready`, send a known CTWA event, and inspect
   only the safe `conversation_context({})` fields.
5. Roll back with `BRAIN_META_ADS_MCP_ENABLED=false`; raw CTWA and CEO service
   continue. Rotate the remote key by replacing the secret and invalidating
   the in-process session.

Tests assert exact unit/env/config snippets, safe probe output and exit codes,
disabled-by-default rollout, and absence of secrets in documentation.

Commit: `docs: add remote Meta MCP rollout and probe`.

## 9. Verification and acceptance checkpoint

- [ ] Run focused tests after each task and keep every task commit small.
- [ ] Run `python -m unittest discover -s tests -v` from the Brain virtualenv.
- [ ] Run `npm test -- --runInBand` in `observers/whatsapp`.
- [ ] Run Ruff on all changed Python files and `git diff --check`.
- [ ] Run the probe tests with fake transport only; a live API key is not
      required for CI.
- [ ] Verify the existing raw CTWA and ordinary-event tests still pass,
      including canonical JSON/base64/integer tags and retention bounds.
- [ ] Verify no test log, exception, SQLite row, context response, or bridge
      fixture contains the service API key or an unbounded remote response.
- [ ] Verify a confirmed projection contains the exact source-derived ad ID,
      ad name, campaign ID, and campaign name; verify mismatches never become
      confirmed.
- [ ] Review the final diff against the approved spec and this plan, then
      create a final integration commit only after all commands report success.

Final integration commit: `feat: integrate remote Meta Ads CTWA attribution`.
