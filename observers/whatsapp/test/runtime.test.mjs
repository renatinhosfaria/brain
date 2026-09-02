import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';

import { HealthState } from '../src/health.mjs';
import { RawAttributionError } from '../src/raw-attribution.mjs';
import * as observerRuntime from '../src/main.mjs';

const { loadObserverConfig, runObserver } = observerRuntime;

const RAW_BODY = 'raw-body-runtime-secret';
const RAW_JID = '15551234567@s.whatsapp.net';
const SAFE_ONE = Object.freeze({
  observer_event_version: 2,
  event_id: `waevt_${'1'.repeat(64)}`,
  observer_device_id: 'observer-test',
  received_at: 1_000,
  remote_jid_hmac: '2'.repeat(64),
  contact_key: '3'.repeat(64),
  body_hmac: '4'.repeat(64),
  body_length: 1,
  native_type: 'conversation',
  transport_kind: 'ordinary_inbound',
});
const SAFE_TWO = Object.freeze({
  ...SAFE_ONE,
  event_id: `waevt_${'5'.repeat(64)}`,
  received_at: 900,
  body_hmac: '6'.repeat(64),
});

function rawMessage(overrides = {}) {
  const { key: keyOverrides = {}, ...messageOverrides } = overrides;
  return {
    key: {
      id: 'raw-message-id',
      remoteJid: RAW_JID,
      fromMe: false,
      ...keyOverrides,
    },
    messageTimestamp: 999,
    pushName: 'Raw Private Name',
    message: { conversation: RAW_BODY },
    ...messageOverrides,
  };
}

function memorySpool(initial = [], calls = []) {
  const records = new Map(initial.map((event) => [event.event_id, structuredClone(event)]));
  return {
    records,
    async ack(eventId) {
      calls.push(`ack:${eventId}`);
      return records.delete(eventId);
    },
    async expireDisplayNames() {
      calls.push('expire');
      return 0;
    },
    async list() {
      calls.push('list');
      return [...records.keys()].reverse();
    },
    async purgeOlderThan() {
      calls.push('purge');
      return { events: 0, quarantine: 0 };
    },
    async put(event) {
      calls.push(`put:${event.event_id}`);
      records.set(event.event_id, structuredClone(event));
      return { event_id: event.event_id, duplicate: false };
    },
    async quarantine(record) {
      calls.push(`quarantine:${record.event_id}`);
      return { event_id: record.event_id, duplicate: false };
    },
    async read(eventId) {
      calls.push(`read:${eventId}`);
      return structuredClone(records.get(eventId));
    },
  };
}

function socketFactory(calls = []) {
  const sockets = [];
  const makeSocket = ({ auth }) => {
    calls.push('makeSocket');
    const ev = new EventEmitter();
    const socket = {
      auth,
      ended: 0,
      ev,
      end() {
        this.ended += 1;
        calls.push('socketEnd');
      },
      sendMessage() {
        throw new Error('receive-only violation');
      },
      readMessages() {
        throw new Error('receive-only violation');
      },
      sendPresenceUpdate() {
        throw new Error('receive-only violation');
      },
    };
    sockets.push(socket);
    return socket;
  };
  return { makeSocket, sockets };
}

function abortableSleep(calls, mode = 'immediate') {
  return (milliseconds, signal) => {
    calls.push(`sleep:${milliseconds}`);
    if (mode === 'immediate') {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const abort = () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      };
      if (signal.aborted) {
        abort();
      } else {
        signal.addEventListener('abort', abort, { once: true });
      }
    });
  };
}

function dependencies(overrides = {}) {
  const calls = overrides.calls ?? [];
  const spool = overrides.spool ?? memorySpool([], calls);
  const sockets = socketFactory(calls);
  const healthState = overrides.healthState ?? new HealthState({ spool, now: () => 2_000 });
  return {
    calls,
    spool,
    sockets,
    options: {
      authState: {
        saveCreds: async () => calls.push('saveCreds'),
        state: { observerAuthOnly: true },
      },
      client: overrides.client ?? {
        async ingest(event) {
          calls.push(`ingest:${event.event_id}`);
          return { event_id: event.event_id, duplicate: false };
        },
      },
      deriveIdentityEvidence:
        overrides.deriveIdentityEvidence ??
        ((message) => {
          calls.push('derive');
          return {
            remoteJid: message.key.remoteJid,
            phoneJid: message.key.remoteJid,
            lid: null,
          };
        }),
      disconnectReasons: {
        badSession: 500,
        connectionReplaced: 440,
        forbidden: 403,
        loggedOut: 401,
        multideviceMismatch: 411,
        restartRequired: 515,
      },
      healthServer: overrides.healthServer,
      healthState,
      ids: overrides.ids ?? {
        eventId(_deviceId, messageId) {
          return messageId === 'raw-capture-failure'
            ? `waevt_${'7'.repeat(64)}`
            : SAFE_ONE.event_id;
        },
      },
      makeSocket: sockets.makeSocket,
      normalize:
        overrides.normalize ??
        (() => {
          calls.push('normalize');
          return structuredClone(SAFE_ONE);
        }),
      now: overrides.now ?? (() => 2_000),
      observerDeviceId: 'observer-test',
      observerSessionDir: '/tmp/observer-runtime-test-session',
      persistLidMapping:
        overrides.persistLidMapping ??
        (async () => {
          calls.push('mapping');
          return { status: 'unresolved' };
        }),
      renderQr: overrides.renderQr ?? ((_qr) => calls.push('qr')),
      rawLimits: overrides.rawLimits,
      quarantineMaxBytes: overrides.quarantineMaxBytes,
      retryBaseMs: overrides.retryBaseMs ?? 10,
      retryMaxAttempts: overrides.retryMaxAttempts ?? 4,
      retryMaxMs: overrides.retryMaxMs ?? 25,
      sleep: overrides.sleep ?? abortableSleep(calls),
      spool,
    },
  };
}

test('valid latest WaWeb result resolves once to an independent version', async () => {
  const sourceVersion = [2, 3000, 1046380673];
  let calls = 0;
  const version = await observerRuntime.resolveWaWebVersion(async () => {
    calls += 1;
    return { version: sourceVersion, isLatest: true };
  });

  assert.equal(calls, 1);
  assert.deepEqual(version, [2, 3000, 1046380673]);
  assert.notStrictEqual(version, sourceVersion);
});

test('WaWeb resolver exception fails closed', async () => {
  await assert.rejects(
    observerRuntime.resolveWaWebVersion(async () => {
      throw new Error('external response must not escape');
    }),
    /latest WhatsApp Web version is unavailable/,
  );
});

for (const [name, result] of [
  ['null result', null],
  ['missing version', { isLatest: true }],
  ['non-array version', { version: '2.3000.1046380673', isLatest: true }],
  ['wrong version length', { version: [2, 3000], isLatest: true }],
  ['non-integer element', { version: [2, 3000, 1046380673.5], isLatest: true }],
  ['non-positive element', { version: [2, 0, 1046380673], isLatest: true }],
  ['isLatest not true', { version: [2, 3000, 1046380673], isLatest: false }],
]) {
  test(`WaWeb resolver rejects ${name}`, async () => {
    await assert.rejects(
      observerRuntime.resolveWaWebVersion(async () => result),
      /latest WhatsApp Web version is unavailable/,
    );
  });
}

test('observer socket factory passes the exact validated version and receive-only flags', () => {
  const calls = [];
  const expectedSocket = { observerSocket: true };
  const version = [2, 3000, 1046380673];
  const makeSocket = observerRuntime.makeObserverSocketFactory((options) => {
    calls.push(options);
    return expectedSocket;
  }, version);

  const auth = { observerAuthOnly: true };
  assert.strictEqual(makeSocket({ auth }), expectedSocket);
  assert.deepEqual(calls, [
    {
      auth,
      version: [2, 3000, 1046380673],
      markOnlineOnConnect: false,
      printQRInTerminal: false,
      syncFullHistory: false,
    },
  ]);
  assert.strictEqual(calls[0].version, version);
});

test('reconnect reuses one resolved WaWeb version without resolving again', async () => {
  let resolverCalls = 0;
  const version = await observerRuntime.resolveWaWebVersion(async () => {
    resolverCalls += 1;
    return { version: [2, 3000, 1046380673], isLatest: true };
  });
  const socketCalls = [];
  const sockets = [];
  const makeSocket = observerRuntime.makeObserverSocketFactory((options) => {
    socketCalls.push(options);
    const socket = { ev: new EventEmitter(), end() {} };
    sockets.push(socket);
    return socket;
  }, version);
  const fixture = dependencies();
  fixture.options.makeSocket = makeSocket;

  const runtime = await runObserver(fixture.options);
  sockets[0].ev.emit('connection.update', {
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode: 515 } } },
  });
  await runtime.idle();
  await runtime.waitForBackground();

  assert.equal(resolverCalls, 1);
  assert.equal(socketCalls.length, 2);
  assert.strictEqual(socketCalls[0].version, version);
  assert.strictEqual(socketCalls[1].version, version);
  await runtime.close();
});

test('invalid WaWeb result prevents socket creation', async () => {
  let socketCalls = 0;
  await assert.rejects(async () => {
    const version = await observerRuntime.resolveWaWebVersion(async () => ({
      version: [2, 3000],
      isLatest: true,
    }));
    const makeSocket = observerRuntime.makeObserverSocketFactory(() => {
      socketCalls += 1;
    }, version);
    makeSocket({ auth: { observerAuthOnly: true } });
  }, /latest WhatsApp Web version is unavailable/);
  assert.equal(socketCalls, 0);
});

test('connection open, QR, creds, and restart-required reconnect use observer auth only', async () => {
  const fixture = dependencies();
  const runtime = await runObserver(fixture.options);
  assert.equal(fixture.sockets.sockets.length, 1);
  assert.deepEqual(fixture.sockets.sockets[0].auth, { observerAuthOnly: true });

  fixture.sockets.sockets[0].ev.emit('creds.update', {});
  fixture.sockets.sockets[0].ev.emit('connection.update', {
    connection: 'connecting',
    qr: 'fake-qr-only',
  });
  fixture.sockets.sockets[0].ev.emit('connection.update', { connection: 'open' });
  await runtime.idle();
  assert.equal(fixture.calls.includes('qr'), true);
  assert.equal(fixture.calls.includes('saveCreds'), true);

  fixture.sockets.sockets[0].ev.emit('connection.update', {
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode: 515 } } },
  });
  await runtime.idle();
  await runtime.waitForBackground();
  assert.equal(fixture.sockets.sockets.length, 2);
  assert.deepEqual(fixture.sockets.sockets[1].auth, { observerAuthOnly: true });
  await runtime.close();
});

test('logged-out observer is fatal and never enters a reconnect loop', async () => {
  const fixture = dependencies();
  const runtime = await runObserver(fixture.options);
  fixture.sockets.sockets[0].ev.emit('connection.update', {
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode: 401 } } },
  });
  await runtime.idle();
  await runtime.waitForBackground();

  assert.equal(fixture.sockets.sockets.length, 1);
  assert.equal(fixture.calls.some((value) => value.startsWith('sleep:')), false);
  assert.equal((await fixture.options.healthState.snapshot()).status, 'unavailable');
  await runtime.close();
});

test('reconnect exponential backoff is bounded', async () => {
  const fixture = dependencies();
  const runtime = await runObserver(fixture.options);
  for (let index = 0; index < 4; index += 1) {
    fixture.sockets.sockets[index].ev.emit('connection.update', {
      connection: 'close',
      lastDisconnect: { error: { output: { statusCode: 515 } } },
    });
    await runtime.idle();
    await runtime.waitForBackground();
  }
  assert.deepEqual(
    fixture.calls.filter((value) => value.startsWith('sleep:')),
    ['sleep:10', 'sleep:20', 'sleep:25', 'sleep:25'],
  );
  await runtime.close();
});

test('shutdown aborts pending reconnect and cleans socket listeners and health', async () => {
  const calls = [];
  const healthServer = {
    async close() {
      calls.push('healthClose');
    },
    async start() {
      calls.push('healthStart');
    },
  };
  const fixture = dependencies({
    calls,
    healthServer,
    sleep: abortableSleep(calls, 'until-abort'),
  });
  const runtime = await runObserver(fixture.options);
  const socket = fixture.sockets.sockets[0];
  socket.ev.emit('connection.update', {
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode: 515 } } },
  });
  await runtime.idle();
  await runtime.close();
  await runtime.waitForBackground();

  assert.equal(fixture.sockets.sockets.length, 1);
  assert.equal(socket.ev.listenerCount('messages.upsert'), 0);
  assert.equal(socket.ev.listenerCount('connection.update'), 0);
  assert.deepEqual(calls.filter((value) => value.startsWith('health')), [
    'healthStart',
    'healthClose',
  ]);
});

test('fromMe and group messages are rejected before identity or normalization', async () => {
  const fixture = dependencies();
  const runtime = await runObserver(fixture.options);
  fixture.calls.length = 0;
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [
      rawMessage({ key: { fromMe: true } }),
      rawMessage({ key: { remoteJid: '123456789@g.us' } }),
    ],
  });
  await runtime.idle();

  assert.equal(fixture.calls.includes('derive'), false);
  assert.equal(fixture.calls.includes('normalize'), false);
  assert.equal(fixture.spool.records.size, 0);
  await runtime.close();
});

test('raw update is converted before the callback returns and queued work is safe-only', async () => {
  const calls = [];
  const delivered = [];
  const fixture = dependencies({
    calls,
    client: {
      async ingest(event) {
        delivered.push(structuredClone(event));
        return { event_id: event.event_id, duplicate: false };
      },
    },
  });
  const runtime = await runObserver(fixture.options);
  calls.length = 0;
  const raw = rawMessage();

  fixture.sockets.sockets[0].ev.emit('messages.upsert', { messages: [raw] });
  assert.deepEqual(calls.slice(0, 2), ['derive', 'normalize']);

  raw.key.remoteJid = 'mutated-after-callback@g.us';
  raw.message.conversation = 'mutated-after-callback';
  await runtime.idle();
  assert.deepEqual(delivered, [SAFE_ONE]);
  await runtime.close();
});

test('multiple messages follow identity, mapping, normalize, spool, ingest, ack order', async () => {
  const calls = [];
  let normalized = 0;
  const fixture = dependencies({
    calls,
    deriveIdentityEvidence(message) {
      calls.push(`derive:${message.key.id}`);
      return {
        remoteJid: '123456789012345@lid',
        phoneJid: RAW_JID,
        lid: '123456789012345@lid',
      };
    },
    normalize() {
      normalized += 1;
      calls.push(`normalize:${normalized}`);
      return structuredClone(normalized === 1 ? SAFE_ONE : SAFE_TWO);
    },
    async persistLidMapping() {
      calls.push('mapping');
      return { status: 'written' };
    },
  });
  const runtime = await runObserver(fixture.options);
  calls.length = 0;
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [
      rawMessage({ key: { id: 'one' } }),
      rawMessage({ key: { id: 'two' } }),
    ],
  });
  await runtime.idle();

  assert.deepEqual(calls, [
    'derive:one',
    'mapping',
    'normalize:1',
    `put:${SAFE_ONE.event_id}`,
    `ingest:${SAFE_ONE.event_id}`,
    `ack:${SAFE_ONE.event_id}`,
    'derive:two',
    'mapping',
    'normalize:2',
    `put:${SAFE_TWO.event_id}`,
    `ingest:${SAFE_TWO.event_id}`,
    `ack:${SAFE_TWO.event_id}`,
  ]);
  await runtime.close();
});

test('raw attribution failures are quarantined and do not interrupt a subsequent ordinary message', async () => {
  const calls = [];
  const quarantined = [];
  const delivered = [];
  const spool = memorySpool([], calls);
  spool.quarantine = async (record) => {
    calls.push(`quarantine:${record.event_id}`);
    quarantined.push(structuredClone(record));
    return { event_id: record.event_id, duplicate: false };
  };
  const fixture = dependencies({
    calls,
    spool,
    client: {
      async ingest(event) {
        delivered.push(structuredClone(event));
        return { event_id: event.event_id, duplicate: false };
      },
    },
    quarantineMaxBytes: 1_024,
    rawLimits: { maxBytes: 16, maxDepth: 32, maxNodes: 10_000 },
    normalize(message, _capturedAt, _ids, _deviceId, rawLimits) {
      assert.deepEqual(rawLimits, {
        maxBytes: 16,
        maxDepth: 32,
        maxNodes: 10_000,
      });
      if (message.key.id === 'raw-capture-failure') {
        throw new RawAttributionError('raw_size');
      }
      return structuredClone(SAFE_TWO);
    },
  });
  const runtime = await runObserver(fixture.options);
  calls.length = 0;
  const written = [];
  const originalWrite = process.stderr.write;
  process.stderr.write = (chunk) => {
    written.push(String(chunk));
    return true;
  };
  try {
    fixture.sockets.sockets[0].ev.emit('messages.upsert', {
      messages: [
        rawMessage({
          key: { id: 'raw-capture-failure' },
          message: {
            extendedTextMessage: {
              text: 'ordinary text',
              contextInfo: {
                externalAdReply: { sourceType: 'ad', sourceId: 'raw-only' },
              },
            },
          },
        }),
        rawMessage({ key: { id: 'ordinary-after-raw-failure' } }),
      ],
    });
    await runtime.idle();
  } finally {
    process.stderr.write = originalWrite;
  }

  assert.equal(quarantined.length, 1);
  assert.equal(quarantined[0].event_id, `waevt_${'7'.repeat(64)}`);
  assert.equal(quarantined[0].reason, 'raw_size');
  assert.equal(quarantined[0].external_ad_reply_raw.sourceId, 'raw-only');
  assert.deepEqual(delivered, [SAFE_TWO]);
  assert.equal((await fixture.options.healthState.snapshot()).raw_capture_failure_count, 1);
  assert.equal(written.join('').includes('raw-only'), false);
  assert.equal(written.join('').includes('raw_size'), true);
  await runtime.close();
});

test('raw attribution that makes quarantine exceed its ceiling stores only technical metadata', async () => {
  const quarantined = [];
  const spool = memorySpool();
  spool.quarantine = async (record) => {
    quarantined.push(structuredClone(record));
    return { event_id: record.event_id, duplicate: false };
  };
  const fixture = dependencies({
    quarantineMaxBytes: 256,
    rawLimits: { maxBytes: 16, maxDepth: 32, maxNodes: 10_000 },
    spool,
    normalize() {
      throw new RawAttributionError('raw_size');
    },
  });
  const runtime = await runObserver(fixture.options);
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [
      rawMessage({
        key: { id: 'raw-capture-failure' },
        message: {
          extendedTextMessage: {
            text: 'ordinary text',
            contextInfo: {
              externalAdReply: { sourceId: 'x'.repeat(80) },
            },
          },
        },
      }),
    ],
  });
  await runtime.idle();

  assert.equal(quarantined.length, 1);
  assert.equal(quarantined[0].reason, 'raw_size');
  assert.equal(quarantined[0].external_ad_reply_raw, null);
  await runtime.close();
});

test('runtime stamps normalized messages as observer event version 2', async () => {
  const delivered = [];
  const unversioned = { ...SAFE_ONE };
  delete unversioned.observer_event_version;
  const fixture = dependencies({
    client: {
      async ingest(event) {
        delivered.push(structuredClone(event));
        return { event_id: event.event_id, duplicate: false };
      },
    },
    normalize: () => structuredClone(unversioned),
  });
  const runtime = await runObserver(fixture.options);
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [rawMessage()],
  });
  await runtime.idle();

  assert.equal(delivered[0].observer_event_version, 2);
  await runtime.close();
});

test('Brain durable new and duplicate success both ACK only after ingest', async () => {
  for (const duplicate of [false, true]) {
    const calls = [];
    const fixture = dependencies({
      calls,
      client: {
        async ingest(event) {
          calls.push('ingest');
          return { event_id: event.event_id, duplicate };
        },
      },
    });
    const runtime = await runObserver(fixture.options);
    calls.length = 0;
    fixture.sockets.sockets[0].ev.emit('messages.upsert', {
      messages: [rawMessage()],
    });
    await runtime.idle();
    assert.equal(calls.indexOf('ingest') < calls.findIndex((value) => value.startsWith('ack:')), true);
    assert.equal(fixture.spool.records.size, 0);
    await runtime.close();
  }
});

test('retryable and identity-unavailable failures never ACK and retry only safe spool data', async () => {
  for (const status of [500, 503]) {
    const calls = [];
    let attempts = 0;
    const seen = [];
    const fixture = dependencies({
      calls,
      client: {
        async ingest(event) {
          attempts += 1;
          seen.push(structuredClone(event));
          if (attempts === 1) {
            const error = new Error('safe retryable failure');
            error.retryable = true;
            error.status = status;
            throw error;
          }
          return { event_id: event.event_id, duplicate: false };
        },
      },
      retryMaxAttempts: 1,
    });
    const runtime = await runObserver(fixture.options);
    fixture.sockets.sockets[0].ev.emit('messages.upsert', {
      messages: [rawMessage()],
    });
    await runtime.idle();
    assert.equal(fixture.spool.records.has(SAFE_ONE.event_id), true);
    await runtime.waitForBackground();
    assert.equal(fixture.spool.records.has(SAFE_ONE.event_id), false);
    assert.equal(seen.length, 2);
    for (const event of seen) {
      const serialized = JSON.stringify(event);
      assert.equal(serialized.includes(RAW_BODY), false);
      assert.equal(serialized.includes(RAW_JID), false);
      assert.deepEqual(event, SAFE_ONE);
    }
    await runtime.close();
  }
});

test('permanent 400 is retained and suppressed without sleep or tight retry', async () => {
  const calls = [];
  const fixture = dependencies({
    calls,
    client: {
      async ingest() {
        calls.push('permanent');
        const error = new Error('permanent');
        error.retryable = false;
        error.status = 400;
        throw error;
      },
    },
  });
  const runtime = await runObserver(fixture.options);
  calls.length = 0;
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [rawMessage()],
  });
  await runtime.idle();
  await runtime.drain();

  assert.equal(fixture.spool.records.has(SAFE_ONE.event_id), true);
  assert.equal(calls.filter((value) => value === 'permanent').length, 1);
  assert.equal(calls.some((value) => value.startsWith('ack:')), false);
  assert.equal(calls.some((value) => value.startsWith('sleep:')), false);
  assert.equal((await fixture.options.healthState.snapshot()).permanent_failure_count, 1);
  await runtime.close();
});

test('startup and reconnect drain safe events oldest-first after expiry and purge', async () => {
  const calls = [];
  const spool = memorySpool([SAFE_ONE, SAFE_TWO], calls);
  const fixture = dependencies({ calls, spool });
  const runtime = await runObserver(fixture.options);

  assert.deepEqual(calls.slice(0, 3), ['expire', 'purge', 'list']);
  assert.deepEqual(
    calls.filter((value) => value.startsWith('ingest:')),
    [`ingest:${SAFE_TWO.event_id}`, `ingest:${SAFE_ONE.event_id}`],
  );

  spool.records.set(SAFE_ONE.event_id, structuredClone(SAFE_ONE));
  calls.length = 0;
  fixture.sockets.sockets[0].ev.emit('connection.update', { connection: 'open' });
  await runtime.idle();
  assert.deepEqual(calls.slice(0, 3), ['expire', 'purge', 'list']);
  assert.equal(calls.includes(`ingest:${SAFE_ONE.event_id}`), true);
  await runtime.close();
});

test('purged count and unresolved identity are aggregate-only health state', async () => {
  const calls = [];
  const spool = memorySpool([], calls);
  spool.purgeOlderThan = async () => ({ events: 3, quarantine: 2 });
  const unresolvedSafe = { ...SAFE_ONE };
  delete unresolvedSafe.contact_key;
  const fixture = dependencies({
    calls,
    normalize: () => structuredClone(unresolvedSafe),
    spool,
  });
  const runtime = await runObserver(fixture.options);
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [rawMessage({ key: { remoteJid: '123456789012345@lid' } })],
  });
  await runtime.idle();
  const health = await fixture.options.healthState.snapshot();

  assert.equal(health.purged_event_count, 3);
  assert.equal(health.unresolved_identity_count, 1);
  assert.equal(JSON.stringify(health).includes(RAW_JID), false);
  assert.equal(JSON.stringify(health).includes(SAFE_ONE.event_id), false);
  await runtime.close();
});

test('mapping failure strips contact proof before safe persistence and delivery', async () => {
  const seen = [];
  const fixture = dependencies({
    client: {
      async ingest(event) {
        seen.push(structuredClone(event));
        const error = new Error('identity unavailable');
        error.retryable = true;
        error.status = 503;
        throw error;
      },
    },
    deriveIdentityEvidence: () => ({
      remoteJid: '123456789012345@lid',
      phoneJid: RAW_JID,
      lid: '123456789012345@lid',
    }),
    persistLidMapping: async () => ({ status: 'conflict' }),
    retryMaxAttempts: 0,
  });
  const runtime = await runObserver(fixture.options);
  fixture.sockets.sockets[0].ev.emit('messages.upsert', {
    messages: [rawMessage()],
  });
  await runtime.idle();

  assert.equal('contact_key' in seen[0], false);
  assert.equal('contact_key' in fixture.spool.records.get(SAFE_ONE.event_id), false);
  await runtime.close();
});

test('runtime close ignores later events and never invokes send/read/presence APIs', async () => {
  const fixture = dependencies();
  const runtime = await runObserver(fixture.options);
  const socket = fixture.sockets.sockets[0];
  await runtime.close();
  socket.ev.emit('messages.upsert', { messages: [rawMessage()] });
  await runtime.idle();

  assert.equal(fixture.spool.records.size, 0);
  assert.equal(socket.ended, 1);
});

test('production config requires own observer subtree and transport-only secrets', () => {
  const valid = {
    BRAIN_OBSERVER_DEVICE_ID: 'observer-prod-a',
    BRAIN_OBSERVER_HEALTH_HOST: '127.0.0.1',
    BRAIN_OBSERVER_HEALTH_PORT: '8775',
    BRAIN_OBSERVER_OUTBOX_DIR: '/var/lib/brain/whatsapp-observer/outbox',
    BRAIN_OBSERVER_SESSION_DIR: '/var/lib/brain/whatsapp-observer/session',
    BRAIN_OBSERVER_TOKEN: 'observer-token-placeholder',
    BRAIN_TRANSPORT_HMAC_SECRET: 't'.repeat(32),
    BRAIN_URL: 'http://127.0.0.1:8765',
  };
  const parsed = loadObserverConfig(valid);
  assert.equal(parsed.healthHost, '127.0.0.1');
  assert.deepEqual(parsed.rawLimits, {
    maxBytes: 4 * 1024 * 1024,
    maxDepth: 32,
    maxNodes: 10_000,
  });
  assert.equal(parsed.quarantineMaxBytes, 32 * 1024 * 1024);
  assert.equal('runtimeHmacSecret' in parsed, false);
  assert.throws(
    () =>
      loadObserverConfig({
        ...valid,
        BRAIN_OBSERVER_SESSION_DIR: '/root/.hermes/platforms/whatsapp/session',
      }),
    /observer subtree|session/i,
  );
  assert.throws(
    () =>
      loadObserverConfig({ ...valid, BRAIN_OBSERVER_HEALTH_HOST: '0.0.0.0' }),
    /127\.0\.0\.1|health/i,
  );
  assert.throws(
    () => loadObserverConfig({ ...valid, BRAIN_CTWA_RAW_MAX_DEPTH: '0' }),
    /raw|max depth/i,
  );
  const overridden = loadObserverConfig({
    ...valid,
    BRAIN_CTWA_RAW_MAX_BYTES: '1024',
    BRAIN_CTWA_RAW_MAX_DEPTH: '4',
    BRAIN_CTWA_RAW_MAX_NODES: '100',
    BRAIN_CTWA_QUARANTINE_MAX_BYTES: '2048',
  });
  assert.deepEqual(overridden.rawLimits, {
    maxBytes: 1_024,
    maxDepth: 4,
    maxNodes: 100,
  });
  assert.equal(overridden.quarantineMaxBytes, 2_048);
});
