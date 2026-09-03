# CTWA Attribution via Remote Meta Ads MCP

**Status:** design approved for review  
**Date:** 2026-09-02

## Goal

Connect Brain to the existing `mcp-meta-ads` deployment over HTTPS so a
CTWA `sourceId` captured by the WhatsApp Observer can be resolved to the exact
Meta ad and its campaign. Persist the confirmation in Brain and expose it to
the CEO through `conversation_context({})`, without changing the remote MCP
repository.

The current raw CTWA capture path remains authoritative for transport evidence
and continues to work when the remote MCP is unavailable.

## Non-goals

- Do not modify or deploy the `mcp-meta-ads` repository.
- Do not use Meta's hosted MCP or its OAuth/DCR flow.
- Do not call Graph API directly from Brain.
- Do not expose arbitrary remote MCP tools to Hermes or the CEO.
- Do not infer attribution from campaign names, chronology, or fuzzy matches.
- Do not claim that a current `ACTIVE` status proves the ad was active at the
  historical click time.

## Remote MCP contract

Brain connects to the operator-provided endpoint:

```text
https://mcp-facebook-ads.famachat.com.br/mcp
```

The connection uses MCP Streamable HTTP and sends:

```text
Authorization: Bearer $BRAIN_META_ADS_MCP_API_KEY
```

The remote server owns the Meta access token and its Graph API configuration.
Brain never receives or stores that Meta token.

The client is permitted to call only these existing read tools:

- `meta_list_ad_accounts`
- `meta_get_ad`
- `meta_get_campaign`

The remote server currently exposes additional read and write tools, but their
presence does not grant Brain permission to invoke them.

## Configuration

Add explicit Brain settings:

```text
BRAIN_META_ADS_MCP_ENABLED=false
BRAIN_META_ADS_MCP_URL=https://mcp-facebook-ads.famachat.com.br/mcp
BRAIN_META_ADS_MCP_API_KEY=REPLACE_WITH_SECRET
BRAIN_META_AD_ACCOUNT_ID=act_1598606388477916
BRAIN_META_ADS_MCP_TIMEOUT_SECONDS=4
BRAIN_META_ADS_MCP_RESPONSE_MAX_BYTES=8388608
```

The URL must be HTTPS and match the configured host exactly. Redirects,
non-HTTPS URLs, userinfo, query credentials, and alternate hosts are rejected.
The API key is accepted only from the service secret environment and is never
written to SQLite, context output, logs, command arguments, or exception text.
The feature remains disabled until the operator enables it after a successful
probe.

## Client lifecycle and probe

`RemoteMetaAdsMcpClient` is a private Brain dependency, not an MCP tool exposed
to Hermes. It uses the installed Python MCP SDK's Streamable HTTP transport.

The client maintains at most one session per Brain process, guarded by an
async lock. A session is initialized lazily and recreated after a transport,
protocol, or authentication failure. The client never refreshes or stores the
remote Meta token.

The first operation performs a bounded probe:

1. initialize a Streamable HTTP MCP session;
2. call `tools/list` and require the three permitted tool names;
3. call `meta_list_ad_accounts`;
4. require the configured account `act_1598606388477916` to be the only
   account returned by the token's accessible account set;
5. mark the remote dependency ready only after all checks succeed.

An absent, extra, or differently formatted account is an account mismatch and
is not silently accepted. Probe results contain only aggregate health state;
they never include the API key or remote response text.

## Exact resolution flow

For each eligible CTWA event with an original decimal `sourceId`:

1. Brain reads the original source ID from the validated raw CTWA capture.
2. It calls `meta_get_ad` with that exact ID.
3. It requires the returned `id` to equal the source ID byte-for-byte.
4. It requires a non-empty ad name, `campaign_id`, and a valid ad status.
5. It calls `meta_get_campaign` with the returned campaign ID.
6. It requires the returned campaign `id` to equal the ad's campaign ID and a
   non-empty campaign name.
7. It requires `effective_status == "ACTIVE"` for both ad and campaign. The
   active-only policy is mandatory for this integration.
8. It persists the immutable event-to-ad binding and the confirmed names.

The account probe is the account-boundary proof because the existing remote
tools do not return an account field in `meta_get_ad`. The deployment contract
therefore requires the remote Meta token to expose exactly the configured ad
account; a token with multiple accessible accounts is rejected during probe.

If the ad is inactive, archived, missing, malformed, belongs to an inaccessible
account, or any exact comparison fails, the event is not confirmed. It remains
pending or unavailable with a bounded retry state.

The current status check describes the state observed during resolution. It is
not historical proof of the state at the moment of the WhatsApp click. A later
historical-insights feature would be a separate design.

## Durable state

Add a Brain-owned attribution table associated with `transport_events`:

- one immutable event binding per CTWA event;
- original source ID representation needed for exact lookup;
- status `pending`, `confirmed`, or `unavailable`;
- confirmed ad/campaign IDs and names only after exact validation;
- bounded error code, retry timestamp, attempt count, and lease ownership;
- creation/update timestamps and retention tied to the transport event.

The table must be transactional with CTWA ingestion when an eligible event is
first received, idempotent on replay, and deleted when its parent transport
event expires. A worker processes due rows with a durable lease so concurrent
Brain processes cannot issue duplicate lookups or overwrite a confirmed
binding.

The worker uses bounded timeouts, exponential retry, and an account-level
authentication circuit. A failed probe or lookup never blocks transport ACK,
conversation context, or CEO atendimento.

## CEO context contract

`conversation_context({})` retains the existing contact, event, and raw
`external_ad_reply` fields. For each recent CTWA event it may additionally
return:

```json
{
  "meta_attribution": {
    "status": "confirmed",
    "ad_id": "...",
    "ad_name": "...",
    "campaign_id": "...",
    "campaign_name": "..."
  }
}
```

Pending and unavailable states contain only their bounded status/reason and no
unconfirmed names. Ordinary events never carry Meta attribution. The bridge
validates the expanded shape and still rejects unknown fields, malformed IDs,
and attribution attached to a non-CTWA event. Remote errors are translated to
safe unavailable reasons before reaching Hermes.

## Failure and security behavior

- DNS/TLS/HTTP/MCP failures: bounded `unavailable` or pending retry.
- 401/403: open the durable authentication circuit; do not retry in a tight
  loop and do not fall back to another credential source.
- malformed tool result or unexpected tool schema: `meta_invalid_response`.
- account mismatch: `meta_account_mismatch`.
- exact ID/name/status failure: leave pending; never fabricate attribution.
- remote latency consumes the remaining `conversation_context` budget and
  cannot extend the CEO's request deadline.
- logs contain event-independent aggregate state only; no source IDs, names,
  API keys, remote payloads, or Meta token material.

## Testing and acceptance

Add tests for:

- HTTPS URL/host validation and API-key header construction;
- Streamable HTTP initialize, session reuse, recreation, and bounded timeout;
- tool allowlist and rejection of write-tool calls;
- account probe success, missing account, extra account, and account mismatch;
- exact source ID, ad ID, campaign ID, ad name, and campaign name checks;
- active-only policy and inactive/archived outcomes;
- malformed, oversized, error, and unauthorized MCP responses;
- durable pending/confirmed/unavailable transitions, lease races, retry, and
  authentication circuit behavior;
- replay idempotency and retention cascade;
- context and CEO bridge delivery of confirmed attribution without token or
  raw-response leakage;
- preserving the existing raw CTWA and ordinary-event behavior.

The existing Python and Observer suites must remain green. The remote MCP
repository is tested independently and is outside this change set.

## Rollout and rollback

1. Configure the remote MCP VPS with its own Meta access token restricted to
   `act_1598606388477916` and verify its HTTPS endpoint.
2. Install the Brain API key as a root-only service secret.
3. Keep `BRAIN_META_ADS_MCP_ENABLED=false` and run the local probe.
4. Enable the feature only after the probe reports the exact account and tool
   contract.
5. Monitor `ready`, `degraded`, `pending`, `confirmed`, and `unavailable`.
6. Roll back by setting `BRAIN_META_ADS_MCP_ENABLED=false`; raw CTWA context
   and CEO atendimento continue without remote calls.

Disabling the feature does not delete raw CTWA evidence. Credential rotation
occurs on the remote MCP VPS and by replacing the Brain API key secret, never
through context or a command-line argument.
