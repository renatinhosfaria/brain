# CTWA WhatsApp Observer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the successful disposable second-Baileys spike into a production receive-only `brain-whatsapp-observer.service` with its own linked-device session, its own pinned Baileys dependency, and a durable outbox containing only privacy-safe HMAC/allowlisted metadata.

**Architecture:** A Node service pins `@whiskeysockets/baileys 7.0.0-rc13`, receives only non-self DM `messages.upsert`, derives a safe event envelope **before durable storage**, atomically spools that safe envelope, POSTs it to Brain's authenticated localhost ingestion route, and deletes it only after durable ACK. The observer shares only the dedicated transport-HMAC key with Brain; it never receives Brain's runtime/turn HMAC key. No raw message text, raw JID/LID, raw observer message ID, raw `sourceId`, raw `ctwaClid`, or full URL is written to the outbox.

**Tech Stack:** Node.js 20+, ESM, `@whiskeysockets/baileys==7.0.0-rc13`, `qrcode-terminal==0.12.0`, built-in `node:test`, built-in crypto/fs/http/fetch.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never read/copy/write/link/reuse `/root/.hermes/platforms/whatsapp/session`.
- Never import Node code from `/usr/local/lib/hermes-agent`; observer owns its exact package lock.
- Observer session: `/var/lib/brain/whatsapp-observer/session`, 0700; credential/mapping files 0600 under `UMask=0077`.
- Safe outbox: `/var/lib/brain/whatsapp-observer/outbox`, 0700; event files 0600.
- Outbox never contains raw body text, raw JID/LID, raw observer message ID, raw `pushName`, raw `sourceId`, raw `ctwaClid`, full URL, thumbnail, `contextInfo`, or opaque WhatsApp payload.
- The optional sanitized display name is the only human-readable WhatsApp profile field allowed in the safe outbox; it carries `display_name_expires_at` and is stripped from pending records after 24 hours.
- `BRAIN_TRANSPORT_HMAC_SECRET` is shared only with Brain and is used to derive `event_id`, `contact_key`, `body_hmac`, `remote_jid_hmac`, and opaque CTWA HMACs.
- Only `msg.key.fromMe === false`, non-group inbound messages enter the pipeline.
- The observer never calls `sendMessage`, `readMessages`, `sendPresenceUpdate`, reactions, group/contact/profile mutation, FamaChat, Kanban, or any LLM.
- Normal Baileys protocol ACK behavior is accepted/documented; no explicit read/receipt APIs are added.
- `messages.upsert` duplicates are safe; Brain deduplicates by `event_id`.
- Unresolved identity is fail-closed and retried from the safe spool using `remote_jid_hmac` plus the observer mapping directory; do not persist the raw JID to make retry easier.
- Reconnect/restart-required (`515`) reuses only observer's own session and never invokes Hermes operations.
- Every task follows TDD and ends with tests plus a focused commit.

---

### Task 1: Scaffold the pinned package and pure raw-to-safe normalizer

**Files:**
- Create: `observers/whatsapp/package.json`
- Create: `observers/whatsapp/package-lock.json`
- Create: `observers/whatsapp/src/hmac.mjs`
- Create: `observers/whatsapp/src/normalize.mjs`
- Create: `observers/whatsapp/test/hmac.test.mjs`
- Create: `observers/whatsapp/test/normalize.test.mjs`
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- `TransportIds(secret)` -> `eventId`, `contactKey`, `bodyHmac`, `jidHmac`, `opaqueHmac`.
- `normalizeInboundMessage(msg, capturedAt, ids, observerDeviceId) -> SafeObserverEvent | null`.
- Raw WhatsApp objects exist only inside the callback/normalizer call stack.

- [x] **Step 1: Pin exact Node dependencies**

`package.json`:

```json
{
  "name": "brain-whatsapp-observer",
  "private": true,
  "type": "module",
  "engines": {"node": ">=20"},
  "scripts": {"test": "node --test test/*.test.mjs"},
  "dependencies": {
    "@whiskeysockets/baileys": "7.0.0-rc13",
    "qrcode-terminal": "0.12.0"
  }
}
```

Generate/commit lockfile with `npm install --package-lock-only --ignore-scripts`; no ranges/latest.

- [x] **Step 2: Write HMAC domain-separation tests**

Use the same canonical formulas as Plan 1. Prove stable event/contact/body/JID HMACs and distinct domains.

- [x] **Step 3: Write safe-normalization tests before implementation**

A historical CTWA fixture must produce a record shaped like:

```javascript
{
  event_id: 'waevt_...',
  observer_device_id: 'observer-a',
  received_at: '...',
  message_timestamp: 123,
  remote_jid_hmac: '...',
  contact_key: '...',          // when phone evidence is already resolvable
  body_hmac: '...',
  body_length: 62,
  display_name: 'Maria',       // sanitized optional, expires separately
  native_type: 'extendedTextMessage',
  transport_kind: 'ctwa_candidate',
  external_ad_reply: {
    source_type: 'ad',
    source_app: 'instagram',
    source_id_present: true,
    source_id_length: 20,
    source_id_hmac: '...',
    source_url_hostname: 'www.instagram.com',
    source_url_length: 80,
    source_url_hmac: '...',
    ctwa_clid_present: true,
    ctwa_clid_length: 40,
    ctwa_clid_hmac: '...',
    show_ad_attribution: true,
    click_to_whatsapp_call: true,
    contains_auto_reply: false
  }
}
```

Serialize the safe event and assert it does **not** contain fixture body, raw JID, raw message ID, raw source ID, raw clid, or full URL.

- [x] **Step 4: Implement only proven text/context paths**

Support the proven `extendedTextMessage.text` and ordinary `conversation` text. Extract only the explicit `contextInfo.externalAdReply` fields required by the spec; never recursively serialize the message tree.

- [x] **Step 5: Add forbidden-call/source regression and CI**

Source test scans observer `src/` for explicit forbidden send/read/presence/reaction calls. CI runs Node 20 `npm ci && npm test`.

- [x] **Step 6: Commit**

```bash
git add observers/whatsapp .gitignore .github/workflows/ci.yml
git commit -m "feat: scaffold privacy-safe WhatsApp observer"
```

---

### Task 2: Produce observer identity mappings and contact proof without raw outbox identity

**Files:**
- Create: `observers/whatsapp/src/identity.mjs`
- Create: `observers/whatsapp/test/identity.test.mjs`
- Modify: `observers/whatsapp/src/normalize.mjs`

**Interfaces:**
- `deriveIdentityEvidence(msg) -> {remoteJid, phoneJid|null, lid|null}` in memory only.
- `persistLidMapping(sessionDir, evidence)` writes the exact mapping-file semantics Brain already validates.
- `contactKeyForEvidence(evidence, transportIds) -> string|null`.

- [x] **Step 1: Write fail-closed identity tests**

Test direct PN JID, LID plus one validated PN-alt field, malformed/group IDs, and conflicting PN alternatives. Accept only numeric `@s.whatsapp.net` and numeric `@lid` patterns.

- [x] **Step 2: Implement atomic mapping writes**

Use temp file + fsync + rename, 0600. Filenames/values match Brain's mapping resolver semantics (`lid-mapping-{phone}.json`, `lid-mapping-{lid}_reverse.json`). Existing conflicting evidence is never overwritten; event remains unresolved/retryable.

- [x] **Step 3: Keep raw identity in memory only**

Before returning the safe event, derive `remote_jid_hmac` and, where resolvable, `contact_key`. Remove raw identity/message IDs from the returned object.

- [x] **Step 4: Run and commit**

```bash
cd observers/whatsapp && npm test
cd /root/brain
git add observers/whatsapp/src/identity.mjs observers/whatsapp/test/identity.test.mjs observers/whatsapp/src/normalize.mjs
git commit -m "feat: persist observer identity evidence safely"
```

---

### Task 3: Add atomic raw-data-free outbox and authenticated Brain client

**Files:**
- Create: `observers/whatsapp/src/spool.mjs`
- Create: `observers/whatsapp/src/brain-client.mjs`
- Create: `observers/whatsapp/test/spool.test.mjs`
- Create: `observers/whatsapp/test/brain-client.test.mjs`

**Interfaces:**
- `SafeSpool(rootDir)` -> `put`, `list`, `read`, `ack`, `purgeOlderThan`, `expireDisplayNames`.
- `BrainClient.ingest(safeEvent)` -> `{event_id, duplicate}`.

- [x] **Step 1: Write outbox privacy tests**

Persist a safe event derived from a fixture whose raw body/JID/message ID/source IDs are known to the test. Read every outbox byte and assert none of those raw values occur. Assert only allowlisted JSON keys exist.

- [x] **Step 2: Implement atomic event files**

File name is HMAC event ID, not raw message ID. Write to `*.tmp` with mode 0600, fsync, rename. Safe event retention maximum 72 hours. On every scan, if `display_name_expires_at <= now`, rewrite the pending record without the display name before retransmission.

- [x] **Step 3: Write Brain HTTP client tests**

Local fake server asserts exact route `/internal/transport/events`, bearer observer token, bounded timeout, JSON body equal to safe event, retry on network/5xx, controlled drop/quarantine on permanent 4xx schema rejection.

- [x] **Step 4: Implement and run tests**

```bash
cd observers/whatsapp && npm test
```

- [x] **Step 5: Commit**

```bash
git add observers/whatsapp/src/spool.mjs observers/whatsapp/src/brain-client.mjs observers/whatsapp/test
git commit -m "feat: add privacy-safe observer outbox"
```

---

### Task 4: Build Baileys runtime, retry loop, health, and systemd deployment

**Files:**
- Create: `observers/whatsapp/src/main.mjs`
- Create: `observers/whatsapp/src/health.mjs`
- Create: `observers/whatsapp/test/health.test.mjs`
- Create: `observers/whatsapp/test/runtime.test.mjs`
- Create: `deploy/brain-whatsapp-observer.service`
- Create: `deploy/brain-whatsapp-observer.env.example`
- Modify: `docs/runbook.md`
- Modify: `tests/test_deployment_contracts.py`

**Interfaces:**
- Env: `BRAIN_OBSERVER_SESSION_DIR`, `BRAIN_OBSERVER_OUTBOX_DIR`, `BRAIN_OBSERVER_TOKEN`, `BRAIN_OBSERVER_DEVICE_ID`, `BRAIN_TRANSPORT_HMAC_SECRET`, `BRAIN_URL`.
- Health `127.0.0.1:8775/health`: status, WhatsApp connection, outbox depth/oldest age, unresolved-identity count; no PII.

- [x] **Step 1: Implement/test connection lifecycle with dependency injection**

`runObserver({makeSocket, authState, ids, normalize, spool, client})` allows tests without network. QR renders through `qrcode-terminal`. Restart-required reconnects with observer session; never programmatic logout of another device.

- [x] **Step 2: Wire `messages.upsert` raw-to-safe flow**

Order is strict:

```text
receive raw msg in memory
reject fromMe/group
persist any validated mapping evidence
normalize to safe HMAC envelope
zero references to raw msg outside callback scope
safeSpool.put(event)
Brain POST
ACK/remove only after Brain durable success
```

If Brain returns identity-unavailable because mapping is not yet verifiable, keep the safe event and retry after mapping updates. No raw field is reintroduced for retry.

- [x] **Step 3: Drain safe outbox on startup/reconnect**

Oldest-first, bounded exponential backoff; duplicate ACK is success. Expire display names at 24h and events at 72h with non-PII dropped-event counter/alert.

- [x] **Step 4: Add health/source/deployment tests**

Health contains no chat/message/profile values. Unit requires `UMask=0077`, private writable paths only under `/var/lib/brain/whatsapp-observer`, and no Hermes session/source runtime dependency.

- [x] **Step 5: Create hardened systemd unit and run full observer suite**

```bash
cd observers/whatsapp && npm ci && npm test
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_deployment_contracts tests.test_transport_ingest -v
```

- [x] **Step 6: Commit**

```bash
git add observers/whatsapp/src/main.mjs observers/whatsapp/src/health.mjs observers/whatsapp/test deploy/brain-whatsapp-observer.service deploy/brain-whatsapp-observer.env.example docs/runbook.md tests/test_deployment_contracts.py
git commit -m "feat: add production WhatsApp observer service"
```

## Plan 2 ## Bookkeeping

**Audited 2026-09-01.** Every step was unticked while the observer had been
running in production since 2026-08-30. Ticked now against named evidence, all
run on `/opt/brain/node` — the runtime the service executes:

| Task | Proven by |
| --- | --- |
| 1 · pinned package and pure normalizer | `test/normalize.test.mjs` (11), `test/hmac.test.mjs` (9) |
| 2 · identity mappings and contact proof | `test/identity.test.mjs` (17) |
| 3 · atomic outbox and Brain client | `test/spool.test.mjs` (20), `test/brain-client.test.mjs` (15) |
| 4 · Baileys runtime, retry, health, systemd | `test/runtime.test.mjs` (27), `test/health.test.mjs` (6) |

Acceptance Gate

```text
OBSERVER_OWN_BAILEYS_PIN=PASS
OBSERVER_OWN_SESSION=PASS
OBSERVER_FORBIDDEN_SEND_APIS=PASS
OUTBOX_RAW_MESSAGE_TEXT=ABSENT
OUTBOX_RAW_JID_LID=ABSENT
OUTBOX_RAW_SOURCE_IDS=ABSENT
OUTBOX_RESTART_REPLAY=PASS
OBSERVER_IDENTITY_MAPPING=PASS
OBSERVER_RECONNECT=PASS
OBSERVER_HEALTH=PASS
HERMES_COEXISTENCE=PASS
CTWA_INGEST_TO_BRAIN=PASS
HERMES_CORE_FILES_TOUCHED=NO
```

Amendment 2 removed the lifecycle write work this line used to gate, and with it the correlation it asked for. What still holds is the substance: do not treat this plan as finished until a real CTWA reaches Brain and the CEO is served contact-scoped context from it. That proof belongs to Stage 5 of the master plan, because it needs the hook-free plugin deployed, and the proof collected on 2026-08-30 does not carry over — it exercised a turn-correlated contract that no longer exists.
