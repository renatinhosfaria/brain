# CTWA Raw Attribution Capture Design

**Date:** 2026-09-02  
**Status:** Approved in conversation; awaiting written-spec review  
**Repository:** `renatinhosfaria/brain`  
**Related operational repository:** `renatinhosfaria/hermes`

## 1. Purpose

Preserve every value that the installed Baileys version decodes and exposes in
`extendedTextMessage.contextInfo.externalAdReply`, keep that raw attribution
available for the existing transport-retention period, and return it directly
to the Hermes CEO through `conversation_context({})`.

The immediate goal is lossless capture. Resolving `sourceId`, `ctwaClid`, or
other values through the Meta Ads API to obtain campaign, ad-set, or ad names is
explicitly deferred to a later phase.

## 2. Decisions

- Keep raw CTWA attribution alongside the current normalized evidence.
- Preserve original Baileys field names, including fields unknown to Brain's
  current CTWA classifier.
- Preserve binary values and return them to the CEO as tagged Base64 values.
- Store the raw representation as plaintext JSON, not encrypted application
  data.
- Keep current retention: observer spool up to 72 hours, Brain transport events
  up to 90 days, and `conversation_context({})` restricted to the most recent
  six hours.
- Return raw attribution to the CEO directly through
  `conversation_context({})`.
- Keep the current normalized fields and HMACs for classification, validation,
  grouping, and backward compatibility.
- Never silently truncate a raw attribution object. Capture is complete or the
  event is quarantined with a non-sensitive reason.

## 3. Scope boundary

"Complete" means every own data field present on the decoded
`externalAdReply` object delivered to the observer by the installed Baileys
version. The observer cannot preserve protocol fields that Baileys itself does
not know and discards before producing that object. A future Baileys upgrade
will automatically make newly decoded enumerable fields eligible for capture
without requiring a Brain allowlist change.

This phase does not:

- call the Meta Ads API;
- translate raw identifiers into campaign, ad-set, or ad names;
- persist the complete WhatsApp message, `contextInfo`, message body, JID, LID,
  session credentials, or unrelated message fields;
- change the definition of `ctwa_candidate`;
- change CRM lifecycle behavior.

## 4. Architecture

The observer produces two representations from one `externalAdReply`:

1. `external_ad_reply` remains the current normalized, allowlisted evidence
   used to calculate and validate `transport_kind`.
2. `external_ad_reply_raw` is a recursively serialized representation of the
   complete decoded object.

The data flow is:

```text
Baileys externalAdReply
  -> recursive lossless JSON-compatible serialization
  -> observer event.external_ad_reply_raw
  -> durable observer spool
  -> authenticated Brain ingestion
  -> transport_events.external_ad_reply_raw_json
  -> conversation_context({}) event.external_ad_reply
  -> Hermes CEO
```

The normalized representation remains authoritative for transport
classification. Raw fields are attribution evidence and future lookup input;
they do not independently alter classification or lifecycle semantics.

## 5. Raw serialization contract

Object keys retain the exact spelling delivered by Baileys, normally camelCase.
The serializer accepts objects, arrays, strings, finite numbers, booleans, and
`null`. It walks own data properties only and does not invoke getters or copy a
prototype.

Binary values (`Buffer` and `Uint8Array`) use this unambiguous representation:

```json
{
  "$type": "bytes",
  "encoding": "base64",
  "data": "AAECAw=="
}
```

If Baileys later supplies an integer that cannot be represented losslessly as a
JSON number, it uses:

```json
{
  "$type": "integer",
  "encoding": "decimal",
  "data": "9223372036854775807"
}
```

The stored JSON is canonical and compact: UTF-8, no insignificant whitespace,
stable key ordering, and no `NaN` or infinity. Canonicalization allows duplicate
`event_id` ingestion to compare the complete raw payload deterministically.

Unsupported values, cycles, accessor properties, or invalid Unicode cause the
whole raw capture to fail. The observer does not stringify such values
implicitly or replace them with placeholders.

## 6. Observer and spool contract

The observer event schema gains the optional top-level field
`external_ad_reply_raw`. It is required whenever the decoded inbound message
contains `externalAdReply`; its absence remains valid for legacy version-1
spool records and ordinary messages.

New spool records use `spool_version: 2`. The reader continues accepting and
delivering valid version-1 records already present during rollout. Version-1
records have no raw attribution and are not retroactively reconstructable.

Before writing a version-2 record, the spool validator independently verifies
the raw tree and its serialized size. The value sent to Brain is the exact value
validated and persisted in the spool; the client must not normalize or rebuild
it a second time.

## 7. Brain ingestion and persistence

`transport_events` gains one nullable column:

```sql
external_ad_reply_raw_json TEXT
```

Runtime database initialization performs an idempotent additive migration for
existing databases. Fresh databases create the column in the base table. Old
rows retain `NULL` and remain valid.

Brain validates the raw tree, canonicalizes it again, and requires it to agree
with the normalized fields used by the classifier for every shared known field.
For example, raw `sourceType` must equal normalized `source_type`, and the raw
`sourceId` must reproduce the normalized presence, length, and HMAC. This
prevents a compromised or defective observer from presenting one value to the
classifier and a different value to the CEO.

Event idempotency comparisons include `external_ad_reply_raw_json`. Reusing an
existing `event_id` with different raw attribution remains a conflict.

Retention deletes the entire transport row after
`transport_retention_days` (90 by default), so the raw JSON follows the existing
transport lifecycle without a second cleanup path.

## 8. CEO response contract

Each event returned by `conversation_context({})` has the stable shape:

```json
{
  "event_id": "waevt_...",
  "transport_kind": "ctwa_candidate",
  "source_app": "instagram",
  "inbound_kind": null,
  "external_ad_reply": {
    "sourceType": "ad",
    "sourceApp": "instagram",
    "sourceId": "original-value",
    "sourceUrl": "https://example.invalid/original",
    "ctwaClid": "original-value",
    "thumbnail": {
      "$type": "bytes",
      "encoding": "base64",
      "data": "..."
    }
  }
}
```

`external_ad_reply` is always present. It is `null` for ordinary events,
legacy events, and any historical row without raw attribution. The response
continues to return at most the existing event-count bound from the most recent
six-hour window, ordered oldest first.

The source and installed copies of `brain-ceo-bridge` accept both the legacy
four-key event during rolling deployment and the new five-key event. Once Brain
is upgraded, the plugin returns the five-key shape to the CEO without filtering
or renaming raw fields.

## 9. Limits and failure behavior

Defaults are configurable but initially set to:

- 4 MiB maximum canonical JSON size for one raw `externalAdReply`;
- 32 maximum nesting levels;
- 10,000 maximum total object properties plus array elements;
- 32 MiB maximum Brain response accepted by `brain-ceo-bridge`.

Request, spool-record, and HTTP-client limits are raised coherently so the
4 MiB raw object plus its event envelope can pass every hop. Limits count UTF-8
bytes after binary values are converted to Base64.

An unsupported or over-limit raw value is a permanent capture failure, not a
retryable network failure. The complete observer input is moved to a protected,
bounded quarantine record that contains the raw `externalAdReply` when it can be
safely encoded within a separate 32 MiB quarantine-record limit and otherwise
contains only the event identifier and a reason code. Quarantine files use the
spool directory's existing restrictive permissions and expire no later than the
72-hour spool retention. The observer records only a reason code and counters
in logs/metrics, never raw values.

The observer remains connected and WhatsApp/Hermes message handling continues.
No normalized-only event is reported as a successful version-2 CTWA capture
when its required raw companion failed.

If Brain stores invalid JSON or the gateway response cannot be validated,
`conversation_context({})` returns the existing controlled `unavailable` form;
it never returns a partially reconstructed attribution object.
If the complete context would exceed 32 MiB, Brain returns
`{"status":"unavailable","reason":"context_too_large"}`. It does not omit an
event, remove a field, or truncate a value to fit the response limit.

## 10. Security and model trust

Plaintext storage is an explicit product decision. Operational protection
therefore depends on the existing `0700` directories, `0600` spool/database
files, loopback-only authenticated APIs, and restricted CEO plugin capability.

Raw values never appear in logs, metrics, audit fields, exception text, health
responses, filenames, or correlation identifiers. Tests use synthetic values
and verify that error paths do not echo them.

Every field inside `external_ad_reply` is untrusted external data. The Hermes
CEO SOUL must state that titles, bodies, URLs, CTA payloads, and other raw values
are evidence only. They cannot supply instructions, authorize actions, alter
routing, trigger tools, or override the CEO's rules. This constraint applies
even when a value resembles a system prompt or operational command.

## 11. Compatibility and rollout

Rollout order prevents the strict current bridge from rejecting the expanded
Brain response:

1. Deploy a bridge version that accepts both legacy and expanded events.
2. Deploy Brain with the additive database migration and expanded response.
3. Deploy the version-2 observer writer.
4. Run synthetic integration checks across observer, spool, ingestion, SQLite,
   gateway API, and bridge.
5. Run one controlled real CTWA capture and verify the original identifiers,
   URLs, text, flags, and thumbnail representation returned to the CEO.
6. Update and verify the installed CEO SOUL trust boundary.

Rollback stops the version-2 observer before reverting Brain. The additive
database column may remain unused. The compatibility bridge can remain deployed.
Any queued version-2 record must be drained by a compatible observer/Brain or
allowed to expire in quarantine; old binaries must not misread it as version 1.

## 12. Test strategy

### Observer tests

- preserve every currently known Baileys `ExternalAdReplyInfo` field;
- preserve additional enumerable fields without an allowlist change;
- preserve nested objects, arrays, `null`, and empty-but-valid scalar values;
- convert `Buffer`, `Uint8Array`, and large integers to tagged lossless values;
- produce deterministic canonical output;
- prove version-1 spool read compatibility and version-2 write behavior;
- reject cycles, accessors, unsupported values, excessive depth/count/size, and
  prove no silent truncation;
- prove raw values do not appear in logs or error messages.

### Brain tests

- migrate an existing database and initialize a fresh database;
- ingest and retrieve a complete synthetic raw object;
- cross-check raw known fields against normalized evidence;
- include raw JSON in duplicate/conflict detection;
- preserve `NULL` behavior for legacy events;
- purge raw JSON with the transport row at retention expiry;
- enforce limits without echoing input.

### Gateway and bridge tests

- return the exact five-key event shape with decoded raw attribution;
- return `external_ad_reply: null` for ordinary and legacy events;
- pass tagged Base64 thumbnail data unchanged;
- accept legacy Brain responses during rollout;
- reject malformed raw trees and oversized responses as controlled unavailable;
- preserve WhatsApp-DM/CEO-only authorization.

### Operational verification

- run the complete Python and observer JavaScript suites;
- run bundle, deployment-contract, plugin-doctor, and integration checks;
- deploy in the required order;
- verify one real CTWA event end to end without printing its sensitive values;
- confirm Hermes continues serving when Brain is unavailable.

## 13. Documentation changes

Update the Brain README, runbook, CTWA design amendment, observer documentation,
bridge README, deployment examples, and the Hermes CEO SOUL. Statements claiming
that raw `sourceId`, raw `ctwaClid`, full URLs, thumbnails, or arbitrary
`externalAdReply` fields are never persisted or exposed must be replaced by the
new plaintext-retention and untrusted-data contract.

## 14. Acceptance criteria

The phase is complete when a controlled CTWA message proves that every field
Baileys exposed in `externalAdReply`, including original `sourceId`, original
`ctwaClid`, full URLs, text, flags, unknown enumerable fields, and thumbnail
bytes, survives observer normalization, spool persistence, Brain ingestion,
SQLite persistence, context retrieval, and bridge validation without value loss
or renaming, and the CEO receives it through `conversation_context({})`.

Legacy data and queued version-1 spool records remain readable, classification
is unchanged, current retention still removes the data on schedule, raw values
do not leak through observability, and no Meta API integration is introduced.
