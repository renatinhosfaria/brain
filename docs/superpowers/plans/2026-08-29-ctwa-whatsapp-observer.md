# CTWA WhatsApp Observer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the successful disposable second-Baileys spike into a production, receive-only `brain-whatsapp-observer.service` that owns its own linked-device session, durably spools privacy-bounded events, and delivers them to Brain's authenticated transport-ingestion route without touching Hermes' WhatsApp session or source tree.

**Architecture:** A small Node service pins its own `@whiskeysockets/baileys 7.0.0-rc13`, receives only non-self DM `messages.upsert`, normalizes to the allowlisted Brain ingestion contract, writes each pending event as an atomic file in a private spool directory, POSTs to localhost Brain, and deletes the file only after Brain acknowledges durable ingestion. A tiny localhost health server exposes connection/outbox state. The observer contains no send/read/presence/reaction APIs and never imports the Baileys package installed under `/usr/local/lib/hermes-agent`.

**Tech Stack:** Node.js 20+, ESM, `@whiskeysockets/baileys==7.0.0-rc13`, `qrcode-terminal==0.12.0`, built-in `node:test`, filesystem atomic rename/fsync, built-in HTTP/fetch.

**Spec:** `docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md`

## Global Constraints

- Never read, copy, write, link, or reuse `/root/.hermes/platforms/whatsapp/session`.
- Never import any Node dependency from `/usr/local/lib/hermes-agent` at runtime.
- Production observer session: `/var/lib/brain/whatsapp-observer/session`, mode `0700`; credential files `0600` under service umask `0077`.
- Pending spool: `/var/lib/brain/whatsapp-observer/outbox`, one atomic JSON file per observer message; no raw body text, raw JID/LID, raw `pushName`, raw `sourceId`, raw `ctwaClid`, full URL, thumbnail, or opaque payload may be written there.
- The body may be sent transiently to Brain over localhost because Brain needs its HMAC for exact turn correlation; the durable spool stores `body` encrypted? No: to preserve restart delivery without persisting raw text, store only the already-normalized body HMAC/length **and** keep the raw body in process memory only. Therefore an event not ACKed before process loss cannot be fully re-correlated from the spool. To satisfy durable recovery without raw-text persistence, the spool must store a one-time HMAC-ready `body_digest_input` encrypted with a local observer spool key, not plaintext. The implementation must use authenticated encryption from Node's built-in crypto (`aes-256-gcm`) and delete ciphertext after ACK; the key lives only in `/etc/brain/observer.env`, never Git. Brain decrypts nothing: the observer decrypts immediately before retransmission and POSTs plaintext only over localhost. This bounded encrypted spool is retained no longer than 72 hours.
- The observer never calls `sendMessage`, `readMessages`, `sendPresenceUpdate`, reactions, group/contact/profile mutation, FamaChat, Kanban, or any LLM.
- Only `msg.key.fromMe === false`, non-group inbound messages are ingested.
- `messages.upsert` duplicate delivery is allowed; Brain deduplicates by `event_id`.
- A protocol reconnect/pairing `515` must reconnect with the observer session, never trigger Hermes operations.
- Every behavior change follows TDD and every task ends with tests plus a commit.

---

### Task 1: Scaffold the pinned observer package and pure event normalizer

**Files:**
- Create: `observers/whatsapp/package.json`
- Create: `observers/whatsapp/package-lock.json`
- Create: `observers/whatsapp/src/normalize.mjs`
- Create: `observers/whatsapp/test/normalize.test.mjs`
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces `normalizeInboundMessage(msg, capturedAt) -> NormalizedObserverEvent | null`.
- `NormalizedObserverEvent` contains only `observer_device_id` later, `message_id`, timestamps, transient `remote_jid`, transient `push_name`, transient `body`, `native_type`, and bounded `external_ad_reply` fields accepted by Plan 1.

- [ ] **Step 1: Create package metadata with exact dependency pins**

`observers/whatsapp/package.json`:

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

Run `npm install --package-lock-only --ignore-scripts` from `observers/whatsapp` and commit the resulting lockfile. Do not use `latest` or a range.

- [ ] **Step 2: Write failing normalization tests**

Use plain object fixtures modeled on the proven CTWA shape:

```javascript
test('normalizes historical CTWA without arbitrary contextInfo', () => {
  const event = normalizeInboundMessage({
    key: {id: 'MSG1', fromMe: false, remoteJid: '123@lid'},
    pushName: 'Maria',
    messageTimestamp: 100,
    message: {
      extendedTextMessage: {
        text: 'Oi',
        contextInfo: {
          externalAdReply: {
            sourceType: 'ad', sourceApp: 'instagram', sourceId: 'source-secret',
            sourceUrl: 'https://www.instagram.com/p/example', ctwaClid: 'clid-secret',
            showAdAttribution: true, clickToWhatsappCall: true,
            containsAutoReply: false, thumbnail: Buffer.from('never-forward')
          },
          mentionedJid: ['do-not-forward@s.whatsapp.net']
        }
      }
    }
  }, '2026-08-29T12:57:00-03:00');
  assert.equal(event.native_type, 'extendedTextMessage');
  assert.equal(event.external_ad_reply.sourceType, 'ad');
  assert.equal(event.external_ad_reply.sourceUrl, 'https://www.instagram.com/p/example');
  assert.equal('thumbnail' in event.external_ad_reply, false);
  assert.equal('contextInfo' in event, false);
});
```

Also prove `fromMe`, groups, empty/non-message events return `null`.

- [ ] **Step 3: Run RED then implement the normalizer**

Run:

```bash
cd observers/whatsapp
npm test
```

Expected: RED before `normalize.mjs`, then PASS after implementation.

Body extraction must support the proven `extendedTextMessage.text` plus ordinary `conversation` text; add only message forms observed/required by tests. Do not serialize arbitrary message trees.

- [ ] **Step 4: Add source-level forbidden-call regression and CI**

Add a test reading all `src/*.mjs` and asserting the forbidden strings are absent:

```javascript
for (const forbidden of ['sendMessage(', 'readMessages(', 'sendPresenceUpdate(', 'sendReaction']) {
  assert.equal(source.includes(forbidden), false, forbidden);
}
```

Update CI with Node 20 setup and `npm ci && npm test` under `observers/whatsapp`.

- [ ] **Step 5: Commit**

```bash
git add observers/whatsapp .gitignore .github/workflows/ci.yml
git commit -m "feat: scaffold receive-only WhatsApp observer"
```

---

### Task 2: Add observer identity evidence and independent LID mapping files

**Files:**
- Create: `observers/whatsapp/src/identity.mjs`
- Create: `observers/whatsapp/test/identity.test.mjs`
- Modify: `observers/whatsapp/src/normalize.mjs`

**Interfaces:**
- Produces `deriveIdentityEvidence(msg) -> {remoteJid, phoneJid|null, lid|null}`.
- Produces `persistLidMapping(sessionDir, evidence)` using the same semantic filenames Brain already understands: `lid-mapping-{phone}.json` and `lid-mapping-{lid}_reverse.json`.

- [ ] **Step 1: Write fail-closed identity tests**

Test direct PN JID, LID with one matching PN-alt field, conflicting alt PNs, groups, and malformed identifiers. The accepted evidence must be numeric PN `@s.whatsapp.net` plus numeric LID `@lid` only.

```javascript
test('lid plus remoteJidAlt creates one proven mapping', () => {
  const evidence = deriveIdentityEvidence({
    key: {remoteJid: '123456789012345@lid', remoteJidAlt: '5534999772714@s.whatsapp.net'}
  });
  assert.deepEqual(evidence, {
    remoteJid: '123456789012345@lid',
    phoneJid: '5534999772714@s.whatsapp.net',
    lid: '123456789012345'
  });
});
```

If the validated Baileys fixture uses `participantAlt` instead, support it only with a corresponding test; do not scan arbitrary nested strings for a phone.

- [ ] **Step 2: Implement atomic mapping writes**

Write JSON string values exactly as Brain's resolver expects, via temp file + fsync + rename, mode `0600`. If existing forward/reverse evidence conflicts, do not overwrite; log only an opaque error code and skip event ingestion until identity can be resolved safely.

- [ ] **Step 3: Run tests**

```bash
cd observers/whatsapp && npm test
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add observers/whatsapp/src/identity.mjs observers/whatsapp/test/identity.test.mjs observers/whatsapp/src/normalize.mjs
git commit -m "feat: persist observer WhatsApp identity mappings"
```

---

### Task 3: Add encrypted durable spool and authenticated Brain delivery

**Files:**
- Create: `observers/whatsapp/src/spool.mjs`
- Create: `observers/whatsapp/src/brain-client.mjs`
- Create: `observers/whatsapp/test/spool.test.mjs`
- Create: `observers/whatsapp/test/brain-client.test.mjs`

**Interfaces:**
- `EncryptedSpool(rootDir, key)` with `put(record)`, `list()`, `read(id)`, `ack(id)`, `purgeOlderThan(cutoff)`.
- `BrainClient({url, token})` with `ingest(event) -> {event_id, duplicate}`.
- Spool files are `outbox/<message-id-hmac>.json` and contain AES-256-GCM ciphertext/IV/tag plus non-sensitive retry timestamps, never plaintext user text/JID/pushName.

- [ ] **Step 1: Write spool encryption tests**

Prove the raw body and JID are absent from bytes on disk but survive decrypt/reload with the correct key. Prove wrong key rejects authentication. Prove `ack()` deletes exactly the target file.

- [ ] **Step 2: Implement AES-256-GCM spool**

Use `crypto.createCipheriv('aes-256-gcm', key, randomBytes(12))`; require a 32-byte key decoded from `BRAIN_OBSERVER_SPOOL_KEY` hex. Atomic write via `open(tmp, 'wx', 0o600)`, write, sync, close, rename.

- [ ] **Step 3: Write Brain client tests with a local HTTP test server**

Assert exact route `/internal/transport/events`, bearer token, JSON content type, bounded 5-second timeout, and retry on network/5xx but not endless tight-loop retry on 4xx.

- [ ] **Step 4: Implement and run tests**

```bash
cd observers/whatsapp && npm test
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add observers/whatsapp/src/spool.mjs observers/whatsapp/src/brain-client.mjs observers/whatsapp/test
git commit -m "feat: add encrypted observer delivery spool"
```

---

### Task 4: Build the Baileys runtime and local health server

**Files:**
- Create: `observers/whatsapp/src/main.mjs`
- Create: `observers/whatsapp/src/health.mjs`
- Create: `observers/whatsapp/test/health.test.mjs`
- Create: `deploy/brain-whatsapp-observer.service`
- Create: `deploy/brain-whatsapp-observer.env.example`
- Modify: `docs/runbook.md`
- Modify: `tests/test_deployment_contracts.py`

**Interfaces:**
- Process consumes `BRAIN_OBSERVER_SESSION_DIR`, `BRAIN_OBSERVER_OUTBOX_DIR`, `BRAIN_OBSERVER_TOKEN`, `BRAIN_OBSERVER_DEVICE_ID`, `BRAIN_OBSERVER_SPOOL_KEY`, `BRAIN_URL`.
- Local health: `GET http://127.0.0.1:8775/health` -> `{status, whatsapp, outbox_depth, oldest_pending_seconds}` without PII.

- [ ] **Step 1: Implement the connection lifecycle using dependency injection**

Keep a small `runObserver({makeSocket, authState, normalize, spool, client})` seam so tests do not connect to WhatsApp. On `connection.update.qr`, render QR using `qrcode-terminal`. On open, set health `connected`. On restart-required close, reconstruct the socket from the same observer session. Never call logout automatically.

- [ ] **Step 2: Wire `messages.upsert`**

For every normalized event: persist any proven LID mapping, put encrypted event into spool, attempt Brain POST, and ACK only after `{status:'ok'}`. On startup/reconnect, drain oldest spool entries first with bounded exponential backoff.

- [ ] **Step 3: Write health and source-contract tests**

Prove health never includes chat/message values. Extend deployment test to assert service hardening includes `UMask=0077`, private `/var/lib/brain/whatsapp-observer`, localhost-only health, and no dependency on Hermes session path.

- [ ] **Step 4: Create systemd unit**

`deploy/brain-whatsapp-observer.service` must use `/usr/bin/node /root/brain/observers/whatsapp/src/main.mjs`, `EnvironmentFile=/etc/brain/observer.env`, `Restart=on-failure`, `UMask=0077`, `NoNewPrivileges=true`, and writable paths only under `/var/lib/brain/whatsapp-observer`.

- [ ] **Step 5: Run full observer/Brain tests**

```bash
cd observers/whatsapp && npm ci && npm test
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest tests.test_deployment_contracts tests.test_transport_ingest -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add observers/whatsapp/src/main.mjs observers/whatsapp/src/health.mjs observers/whatsapp/test deploy/brain-whatsapp-observer.service deploy/brain-whatsapp-observer.env.example docs/runbook.md tests/test_deployment_contracts.py
git commit -m "feat: add production WhatsApp observer service"
```

## Plan 2 Acceptance Gate

Run first in a non-sending production shadow checkpoint with the existing Hermes device still connected:

```text
OBSERVER_OWN_BAILEYS_PIN=PASS
OBSERVER_OWN_SESSION=PASS
OBSERVER_FORBIDDEN_SEND_APIS=PASS
OBSERVER_ENCRYPTED_SPOOL=PASS
OBSERVER_RECONNECT=PASS
OBSERVER_HEALTH=PASS
HERMES_COEXISTENCE=PASS
CTWA_INGEST_TO_BRAIN=PASS
HERMES_CORE_FILES_TOUCHED=NO
```

Do not proceed to lifecycle binding until a real CTWA event reaches Brain and `conversation_context()` correlates it successfully in shadow mode.
