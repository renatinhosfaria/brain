# CTWA Meta Ads Attribution Design

**Date:** 2026-09-02  
**Status:** Approved in conversation; awaiting written-spec review  
**Repository:** `renatinhosfaria/brain`  
**Related operational repository:** `renatinhosfaria/hermes`

## 1. Purpose

Resolve a Click-to-WhatsApp transport event to the exact Meta ad and campaign
that produced it. Brain uses the original `externalAdReply.sourceId` captured
by the WhatsApp observer, verifies that identifier against Meta's Ads MCP
server, persists the result, and exposes it to the Hermes CEO through the
existing `conversation_context({})` call.

The result is evidence, not an inference. Brain may name an ad or campaign only
after Meta returns an ad whose identifier exactly equals the observed
`sourceId` and whose ad account equals the configured account.

The only in-scope ad account is:

```text
act_1598606388477916
```

## 2. Decisions

- Use Meta's hosted Ads MCP server at `https://mcp.facebook.com/ads`.
- Use a hybrid resolver: a local catalog for the common path and a bounded
  live lookup for cache misses.
- Treat the raw CTWA `sourceId` as the primary lookup key.
- Preserve `ctwaClid` as click-level evidence, but do not use it to guess the
  ad or campaign.
- Match identifiers exactly. Never match by name, text, URL, creative content,
  source application, or fuzzy similarity.
- Include ads regardless of their current status and retain the relevant
  catalog history for at least the transport event's 90-day retention period.
- Wait for a bounded live lookup during `conversation_context({})`, but never
  block the commercial conversation when Meta is slow or unavailable.
- Persist a confirmed attribution in Brain and return it to the CEO.
- Do not write the attribution to the CRM in this phase.
- Keep the integration deterministic and read-only. The CEO does not receive a
  direct Meta MCP capability.
- Build authentication and its operational lifecycle from the beginning.

## 3. Scope

### 3.1 In scope

- Meta application and Ads MCP authentication setup for the configured account;
- Ads MCP capability discovery and account-access verification;
- a bounded, strictly validated Ads MCP client inside Brain;
- local ad catalog synchronization;
- durable pending-attribution jobs and retries;
- exact `sourceId` to ad resolution;
- campaign, ad-set, and creative enrichment;
- persistence and retention of confirmed results;
- `conversation_context({})` and CEO bridge contract expansion;
- health, bounded observability, tests, staged rollout, and rollback.

### 3.2 Out of scope

- CRM or Kanban writes;
- Conversions API or offline conversion reporting;
- use of `ctwaClid` to send lead, qualified-lead, or sale events to Meta;
- performance metrics such as spend, impressions, CTR, CPC, or ROAS;
- multiple ad accounts or accounts belonging to other businesses;
- App Review or Advanced Access for third-party business data;
- creating, updating, activating, pausing, or deleting Meta objects;
- a direct Graph/Marketing API fallback;
- changing CTWA classification or lifecycle semantics.

## 4. External facts and trust boundary

Meta's WhatsApp webhook reference represents `referral.source_id` as the ad ID
when the message was triggered by a Click-to-WhatsApp ad. The installed
WhatsApp observer receives the equivalent value as
`externalAdReply.sourceId` and already preserves it without transformation in
`external_ad_reply_raw_json`.

Meta's hosted Ads MCP server exposes structured tools over MCP. The relevant
read tools are:

- `ads_get_ad_accounts` for account discovery and access verification;
- `ads_get_field_context` when the live tool schema requires field/filter
  discovery;
- `ads_get_ad_entities` for retrieving campaigns, ad sets, and ads;
- `ads_get_creatives` only when creative metadata is not included in the
  entity response.

Tool availability is discovered at runtime with MCP `tools/list`; it is not
assumed from documentation alone. Meta states that Ads MCP tool availability is
being rolled out progressively. Missing required tools therefore degrade Meta
attribution without making Brain or WhatsApp unavailable.

All WhatsApp and Meta values are untrusted external data. This includes ad,
ad-set, campaign, and creative names from an account owned by the business.
They may be displayed as evidence but may not supply instructions, choose MCP
tools, change arguments, authorize actions, or alter CEO routing.

## 5. Architecture

### 5.1 `MetaAdsMcpClient`

A new Brain-owned client implements the minimum remote MCP protocol necessary
for `tools/list` and `tools/call`. It has one fixed HTTPS endpoint, a strict
tool allowlist, typed request builders, bounded pagination, response-size
limits, timeouts, and schema validation.

The client is deterministic. No LLM selects a tool or constructs arguments.
Only Brain code may call it, and user-, CEO-, WhatsApp-, and Meta-supplied text
cannot change the endpoint, account, tool name, requested fields, filters, or
pagination controls.

### 5.2 `MetaAdsCatalog`

The catalog stores the latest validated Meta metadata for an ad and its
hierarchy. It makes confirmed context reads local and preserves attribution
when an ad is paused, completed, archived, deleted from ordinary listings, or
renamed after the lead arrives.

The immutable identity is the ad ID. Names and statuses are refreshable
metadata and carry their own fetch timestamp.

### 5.3 `CtwaAttributionResolver`

The resolver consumes only a stored CTWA event. It verifies `sourceType ==
"ad"`, reads the original `sourceId`, and resolves it first from the catalog and
then from Meta when required. It owns the state transition from `pending` to
`confirmed`.

### 5.4 `MetaAttributionWorker`

The worker claims durable jobs, performs retries with backoff, refreshes the
catalog, and updates all pending events sharing the same account and
`sourceId`. No required work lives only in process memory.

### 5.5 `conversation_context({})`

The existing context path reads persisted attribution. If an event is pending,
it may claim one bounded immediate attempt before returning. A failure returns
the contact and CTWA evidence normally with `meta_attribution.status ==
"pending"`; it never converts the whole context response to `unavailable`
solely because Meta failed.

## 6. Persistence model

Runtime database initialization adds three tables by idempotent additive
migration. Foreign-key enforcement remains enabled.

### 6.1 `meta_ads_catalog`

One row represents one ad in one account:

```text
account_id                 canonical account ID without the act_ prefix
ad_id                      Meta ad ID
ad_name                    latest validated name
ad_status                  configured status when supplied
ad_effective_status        effective status when supplied
adset_id                   nullable only when Meta omits it
adset_name                 nullable only when Meta omits it
adset_status               nullable
campaign_id                required for a usable confirmation
campaign_name              required for a usable confirmation
campaign_status            nullable
creative_id                nullable
creative_name              nullable
metadata_complete          boolean
fetched_at                 time of the successful Meta response
last_seen_at               last time Meta returned this ad
PRIMARY KEY (account_id, ad_id)
```

The internal canonical account ID is `1598606388477916`; API and CEO output
render it as `act_1598606388477916`. This normalization is the only accepted
prefix transformation.

### 6.2 `ctwa_meta_attributions`

One row belongs to one transport event:

```text
event_id                   primary key and foreign key to transport_events
account_id                 configured canonical account ID
source_id                  original CTWA sourceId
ctwa_clid                  original CTWA ctwaClid, nullable
status                     pending | confirmed
matched_ad_id              null until confirmed
match_method               null | source_id_exact
metadata_complete          boolean
confirmed_at               nullable
last_attempt_at            nullable
last_error_code            nullable bounded code
created_at
updated_at
```

Database checks require every `confirmed` row to have `matched_ad_id ==
source_id`, `match_method == "source_id_exact"`, and `confirmed_at` present.
The application additionally requires the configured account and mandatory ad
and campaign metadata before performing this transition.

The event-to-ad binding is immutable after confirmation. Later synchronization
may refresh names and statuses in `meta_ads_catalog`, but may not bind the event
to another ad.

Deleting a retained transport event cascades to its attribution row. The raw
CTWA payload and the duplicate lookup fields therefore share the same maximum
retention boundary.

### 6.3 `meta_attribution_jobs`

Jobs are deduplicated by `(account_id, source_id)` and contain attempt count,
next-attempt time, a bounded lease, last error code, and timestamps. Resolving
one job confirms every eligible pending event for that exact pair.

Event insertion, pending-attribution creation, and job creation occur in the
same SQLite transaction. A crash cannot durably store a new resolvable CTWA
event without also storing its pending work.

## 7. Confirmation contract

A resolution becomes `confirmed` only if one validated Meta response proves
all of the following:

1. the CTWA event has `sourceType == "ad"`;
2. the observed `sourceId` is a valid bounded Meta identifier;
3. Meta returns an ad with `ad.id` exactly equal to that `sourceId`;
4. the returned ad belongs to canonical account `1598606388477916`;
5. Meta supplies a non-empty bounded ad name;
6. Meta supplies a campaign ID and non-empty bounded campaign name.

Ad-set and creative metadata are requested and returned when available. Their
absence sets `metadata_complete` to `false` but does not invalidate a proven ad
and campaign. Missing mandatory data leaves the event pending.

`ctwaClid`, `sourceUrl`, headline, body, thumbnail, source application, and any
other field can corroborate or help diagnose the event but can never satisfy a
confirmation condition.

An exact no-result response is not proof of another ad. The event remains
pending with a bounded `meta_not_found` code and is retried according to the
policy below.

## 8. Resolution and synchronization flow

### 8.1 Ingestion

1. Brain validates and stores the transport event as it does today.
2. A lookup-eligible `sourceId` is an ASCII decimal identifier containing 1 to
   64 digits and no sign, whitespace, prefix, or separator.
3. For a raw CTWA object with `sourceType == "ad"` and a lookup-eligible
   `sourceId`, the same transaction creates the attribution row.
4. If the catalog already contains a previously Meta-confirmed row for the
   configured account and exact `sourceId`, the new event is confirmed from
   that durable proof. Otherwise the transaction inserts a pending attribution
   and an idempotent job.
5. Ordinary, legacy, malformed, and non-ad events do not create Meta work.
6. Transport ingestion acknowledges durable capture without waiting for Meta.

### 8.2 Background resolution

1. The worker claims the `(account_id, source_id)` job with a bounded lease.
2. It checks a sufficiently fresh catalog row.
3. On a miss or stale row, it calls Meta.
4. If the live tool supports an exact ID filter, the client uses it.
5. If no exact filter is available, the client reads bounded, paginated account
   results and exact-matches the returned ad ID locally.
6. A valid result updates the catalog and confirms all matching pending rows in
   one transaction.
7. A retryable result releases or reschedules the job without changing the
   attribution to a false terminal answer.

There is no fuzzy or URL fallback and no direct Marketing API fallback in this
phase.

### 8.3 Context-time resolution

When `conversation_context({})` encounters a pending event, it attempts to
claim the shared job. At most one caller performs the Meta request; concurrent
callers use the current persisted state. The attempt receives these initial,
configurable budgets:

- 4 seconds for the Ads MCP operation;
- 5 seconds for the full Brain context operation;
- 7 seconds for the Hermes bridge request.

After the budget, Brain returns `pending` and the worker retains responsibility
for later attempts. The CEO continues the conversation.

### 8.4 Catalog synchronization

- Run an initial synchronization after authentication and before enabling
  resolution in production.
- Refresh active and recently changed ads periodically.
- Run a complete eligible-history sweep daily.
- Include active, paused, campaign-paused, ad-set-paused, completed, rejected,
  archived, and otherwise readable statuses rather than filtering to active.
- Retain a catalog row while any retained attribution references it.
- Garbage-collect an unreferenced row only after it has not been seen for more
  than 90 days.

The implementation plan may tune page size and refresh interval after the
authenticated capability probe, but it may not weaken the exact-match or
90-day history requirements.

## 9. Retry and error behavior

Retryable attempts use jittered backoff with this initial progression:

```text
immediate -> 1 minute -> 5 minutes -> 15 minutes -> 1 hour -> 6 hours
          -> once per 24 hours while the transport event is retained
```

A valid Meta `Retry-After` value takes precedence when it schedules a later
attempt. Retention deletion ends retries for events no longer present.

Bounded public error codes are:

- `meta_timeout`;
- `meta_rate_limited`;
- `meta_server_unavailable`;
- `meta_auth_unavailable`;
- `meta_required_tool_unavailable`;
- `meta_not_found`;
- `meta_incomplete_result`;
- `meta_account_mismatch`;
- `meta_invalid_response`.

Timeouts, rate limits, and `5xx` responses remain pending. Authentication and
authorization failures open a local circuit for at least one hour so Brain
does not repeatedly send an invalid credential. A credential rotation or a
successful explicit health probe closes the circuit.

An account mismatch or structurally inconsistent response never reaches the
CEO as a confirmation. It records only its bounded code and increments a
security counter.

## 10. Ads MCP boundary

The endpoint is fixed in code to `https://mcp.facebook.com/ads`; production
configuration cannot redirect it to an arbitrary host. TLS certificate
validation is mandatory.

The protocol allowlist is limited to MCP initialization, `tools/list`, and
`tools/call`. The tool-name allowlist contains only the read tools enumerated
in section 4. Any other tool name fails locally before a network request.

The client enforces:

- a maximum 8 MiB response per MCP operation;
- bounded field lengths and collection sizes;
- bounded page count and a repeated-cursor detector;
- strict JSON and JSON-RPC envelope validation;
- exact entity and account identifier syntax;
- known nullable fields and explicit rejection of incompatible types;
- no raw response logging or exception echoing.

At startup, the capability probe runs `tools/list`, validates the schemas of
required tools, calls `ads_get_ad_accounts`, and requires the configured account
to be present exactly once. Unsupported schemas or missing tools set the Meta
component to degraded without making Brain's other tools unavailable.

## 11. Authentication and configuration

The setup starts from an operator-controlled browser session:

1. Create or reuse a Meta developer application owned by the business.
2. Add the **Create & manage ads with Ads MCP server** use case.
3. Authorize the business user who has access to the configured ad account.
4. Restrict the Business Integration to read access when Meta exposes that
   choice.
5. For the programmatic Brain client, obtain the user access token through the
   application's OAuth flow or Graph API Explorer as documented by Meta.
6. Grant only the permissions accepted by Meta for the required Ads MCP read
   tools. If the Ads MCP setup requires a broader permission such as
   `ads_management`, compensate with credential isolation and the hard local
   read-tool allowlist; do not expose write calls.
7. Verify the token with `tools/list`, `ads_get_ad_accounts`, and a read of a
   known ad before installation.

Initial configuration fields are:

```text
BRAIN_META_ATTRIBUTION_ENABLED
BRAIN_META_AD_ACCOUNT_ID=act_1598606388477916
BRAIN_META_ADS_MCP_ACCESS_TOKEN          secret; environment/credential only
BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT      non-secret operational timestamp
BRAIN_META_ADS_MCP_TIMEOUT_SECONDS       default 4
BRAIN_META_ADS_SYNC_INTERVAL_SECONDS     configurable after capability probe
```

The token is never accepted from a persisted TOML value, request argument,
database row, or CEO environment. Production should inject it with the same
root-restricted secret mechanism used for existing Brain service credentials;
files containing it must be mode `0600` inside a mode `0700` directory.

Brain warns through bounded health/operational state 14, 7, and 3 days before
the configured expiration timestamp. The runbook includes generation,
validation, atomic rotation, rollback, revocation, and post-rotation probes.
No token fragment or hash is shown to the CEO.

Because only the business's own account is accessed, this phase does not
require a multi-tenant OAuth store, customer consent screen, or Advanced Access
for other businesses.

## 12. CEO response contract

Every event in an expanded `conversation_context({})` response gains the
`meta_attribution` key. It is `null` for ordinary, legacy, non-ad, and otherwise
ineligible events.

### 12.1 Confirmed

```json
{
  "event_id": "waevt_...",
  "transport_kind": "ctwa_candidate",
  "source_app": "instagram",
  "inbound_kind": null,
  "external_ad_reply": {
    "sourceType": "ad",
    "sourceId": "1202...",
    "ctwaClid": "...",
    "sourceUrl": "https://..."
  },
  "meta_attribution": {
    "status": "confirmed",
    "account_id": "act_1598606388477916",
    "matched_by": "source_id_exact",
    "source_id": "1202...",
    "ctwa_clid": "...",
    "ad": {
      "id": "1202...",
      "name": "Meta supplied ad name",
      "status": "ACTIVE"
    },
    "adset": {
      "id": "...",
      "name": "Meta supplied ad-set name",
      "status": "ACTIVE"
    },
    "campaign": {
      "id": "...",
      "name": "Meta supplied campaign name",
      "status": "ACTIVE"
    },
    "creative": {
      "id": "...",
      "name": "Meta supplied creative name"
    },
    "metadata_complete": true,
    "confirmed_at": "2026-09-02T12:00:00Z",
    "metadata_fetched_at": "2026-09-02T12:00:00Z"
  }
}
```

`adset` and `creative` are nullable objects. Their absence requires
`metadata_complete: false`. Status fields are nullable when Meta omits them;
the response prefers effective status where Meta supplies both configured and
effective status.

### 12.2 Pending

```json
{
  "meta_attribution": {
    "status": "pending",
    "source_id": "1202...",
    "ctwa_clid": "...",
    "last_attempt_at": "2026-09-02T12:00:00Z",
    "retry_scheduled": true,
    "last_error_code": "meta_timeout"
  }
}
```

`last_attempt_at` and `last_error_code` are nullable before the first attempt.
`retry_scheduled` is false only when retention has ended or attribution has
been disabled by an operator; a retained eligible event is otherwise retried.

The CEO may assert an ad or campaign only when `status == "confirmed"` and
`matched_by == "source_id_exact"`. `pending` is not evidence for any named Meta
object and never prevents ordinary service.

The bridge accepts the current five-key event and the new six-key event during
rolling deployment. It validates the exact nested shapes, size limits,
timestamps, account pin, equality of `source_id` and `ad.id`, and the bounded
status/error enums before returning data to the CEO.

## 13. Security and observability

- The CEO cannot choose an account, tool, filter, or Meta identifier.
- Raw CTWA and Meta strings cannot become prompts or instructions.
- Meta write tools are absent from the local client interface.
- The configured account is checked before storing or exposing a result.
- Tokens and complete Meta responses never enter logs, metrics, SQLite,
  context output, health output, filenames, or exception text.
- The existing plaintext raw CTWA retention decision remains unchanged.
- The catalog stores only the normalized metadata approved in this spec, not a
  raw Ads MCP response.
- Audit logs contain operation type, decision, bounded error code, duration,
  counts, and retry state, but no token, `ctwaClid`, `sourceId`, ad name,
  campaign name, URL, or response body.

Health reports a separate Meta component state such as `disabled`, `ready`, or
`degraded`. A degraded Meta component does not change the availability of
conversation history, phone resolution, raw CTWA context, or transport
ingestion.

Operational counters include jobs pending, resolutions confirmed, retryable
errors by bounded code, account mismatches, request latency, catalog age, and
credential-expiry state. They contain no attribution values.

## 14. Compatibility, rollout, and rollback

Rollout order is:

1. Create the Meta application/use case and obtain the programmatic credential.
2. Confirm required tools, exact account access, and a known-ad read from the
   deployment host.
3. Deploy a CEO bridge that accepts both current and expanded event contracts.
4. Deploy additive database migrations, the Ads MCP client, resolver, and
   worker with `BRAIN_META_ATTRIBUTION_ENABLED=false`.
5. Run tests and the capability probe.
6. Perform the initial catalog synchronization and verify counts without
   printing attribution data.
7. Enable background resolution and verify synthetic exact-match, mismatch,
   pending, and retry cases.
8. Send one controlled real CTWA message from a fresh contact and compare the
   observed ID, resolved ad, and resolved campaign with Ads Manager.
9. Enable the bounded context-time lookup.
10. Simulate Meta unavailability and credential failure to prove that the CEO
    continues serving with `pending`.

Rollback disables the feature flag and stops the Meta worker. The bridge stays
backward compatible, additive tables remain unused, raw CTWA capture continues,
and no destructive schema rollback is required.

## 15. Test strategy

### 15.1 Ads MCP client

- validate MCP initialization, `tools/list`, and `tools/call` envelopes;
- verify required tool/schema discovery;
- enforce fixed endpoint, account, and read-only tool allowlists;
- test exact-ID filtering and bounded paginated fallback;
- reject malformed JSON-RPC, wrong IDs, wrong accounts, oversized responses,
  repeated cursors, unexpected types, and incomplete mandatory metadata;
- test timeout, rate limit, `Retry-After`, `401`, `403`, and `5xx` behavior;
- prove secrets and returned text never appear in errors or logs.

### 15.2 Persistence and resolver

- initialize fresh databases and migrate existing databases idempotently;
- create event, attribution, and job transactionally;
- preserve jobs across restart and recover expired leases;
- deduplicate multiple events and concurrent attempts for one source ID;
- require exact `sourceId == ad.id` and configured account;
- never confirm from `ctwaClid`, URL, text, or fuzzy matches;
- keep confirmed identity immutable while refreshing metadata;
- cascade event retention and garbage-collect eligible catalog rows;
- test every retry and circuit-breaker branch.

### 15.3 Context and bridge

- preserve current context behavior when the feature is disabled;
- return `meta_attribution: null` for ineligible events;
- return the exact pending and confirmed shapes;
- validate nullable ad-set, creative, and status fields;
- stay within the context and bridge size limits;
- return useful raw CTWA context when Meta is unavailable;
- reject account mismatch, invalid IDs, and inconsistent confirmation shapes;
- prove Meta names cannot influence tool selection or CEO instructions.

### 15.4 Operational verification

- run the complete Python and WhatsApp observer suites;
- run bundle, deployment-contract, bridge, configuration, and migration tests;
- run an authenticated read-only smoke test against the configured account;
- verify a known active ad and a known paused or archived ad;
- verify one real CTWA event end to end without logging its sensitive values;
- verify credential rotation and a deliberately unavailable Meta dependency.

## 16. Acceptance criteria

The phase is complete when all of the following are true:

- authentication is configured from zero and documented for the one approved
  account;
- Brain discovers and validates the required Ads MCP read capabilities;
- a real CTWA `sourceId` is exactly equal to the Meta-returned `ad.id`;
- Meta proves the account, ad name, campaign ID, and campaign name;
- Brain persists an immutable confirmed binding and refreshable metadata;
- `conversation_context({})` returns the approved confirmed shape to the CEO;
- cache misses receive one bounded live attempt and durable background retries;
- active, paused, completed, and archived objects remain resolvable within the
  90-day event window;
- Meta, authentication, and rate-limit failures leave attribution pending while
  the CEO continues serving;
- no fuzzy inference, CRM write, performance metric, conversion upload, Meta
  mutation, cross-account result, or secret exposure occurs;
- rollback can disable all new behavior without impairing raw CTWA capture.

## 17. Primary references

- Meta WhatsApp Cloud API, received message triggered by Click-to-WhatsApp ads:
  <https://www.postman.com/meta/whatsapp-business-platform/request/g7sv9jo/received-message-triggered-by-click-to-whatsapp-ads>
- Meta Ads MCP overview:
  <https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-overview>
- Meta Ads MCP setup and authentication:
  <https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-get-started>
- Meta Ads MCP ad creation and management tool inventory:
  <https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-tools-ad-creation-and-management>
- Meta MCP security and recommendations:
  <https://developers.facebook.com/documentation/mcp>
