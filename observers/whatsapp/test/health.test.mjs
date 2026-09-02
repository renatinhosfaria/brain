import assert from 'node:assert/strict';
import test from 'node:test';

import { createHealthServer, HealthState } from '../src/health.mjs';

const RAW_MARKERS = [
  'waevt_deadbeef',
  '15551234567@s.whatsapp.net',
  '123456789012345@lid',
  '15551234567',
  'Private Display Name',
  'raw-message-id',
  'raw body text',
  'raw-source-id',
  'raw-ctwa-clid',
];

function fakeSpool(events = []) {
  const byId = new Map(events.map((event) => [event.event_id, event]));
  return {
    async list() {
      return [...byId.keys()];
    },
    async read(eventId) {
      return structuredClone(byId.get(eventId));
    },
  };
}

async function withServer(state, callback) {
  const health = createHealthServer({
    host: '127.0.0.1',
    port: 0,
    snapshot: () => state.snapshot(),
  });
  await health.start();
  try {
    const address = health.address();
    assert.equal(address.address, '127.0.0.1');
    await callback(`http://127.0.0.1:${address.port}`, health);
  } finally {
    await health.close();
  }
}

test('health server rejects non-loopback bind and starts only on 127.0.0.1', async () => {
  const state = new HealthState({ spool: fakeSpool(), now: () => 2_000 });
  assert.throws(
    () =>
      createHealthServer({
        host: '0.0.0.0',
        port: 8775,
        snapshot: () => state.snapshot(),
      }),
    /127\.0\.0\.1|loopback/i,
  );
  await withServer(state, async (_baseUrl, health) => {
    assert.equal(health.address().address, '127.0.0.1');
  });
});

test('GET /health returns stable JSON and another path is 404', async () => {
  const state = new HealthState({ spool: fakeSpool(), now: () => 2_000 });
  state.setWhatsApp('connected');
  await withServer(state, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/health`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type'), /^application\/json/);
    assert.deepEqual(await response.json(), {
      status: 'ok',
      whatsapp: 'connected',
      outbox_depth: 0,
      outbox_oldest_age_seconds: 0,
      unresolved_identity_count: 0,
      permanent_failure_count: 0,
      raw_capture_failure_count: 0,
      purged_event_count: 0,
    });

    const missing = await fetch(`${baseUrl}/not-health`);
    assert.equal(missing.status, 404);
  });
});

test('pending safe events degrade health and expose only aggregate metrics', async () => {
  const spool = fakeSpool([
    {
      event_id: 'waevt_deadbeef',
      received_at: 1_700,
      contact_key: 'private-contact-key',
      display_name: 'Private Display Name',
      raw_marker: RAW_MARKERS.join(' '),
    },
    { event_id: 'waevt_other', received_at: 1_900 },
  ]);
  const state = new HealthState({ spool, now: () => 2_000 });
  state.setWhatsApp('connected');
  state.setRetryPending(true);
  state.incrementUnresolvedIdentity();
  state.addPurgedEvents(2);
  state.incrementPermanentFailure();
  state.incrementRawCaptureFailure();

  await withServer(state, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/health`);
    const body = await response.text();
    const payload = JSON.parse(body);

    assert.equal(payload.status, 'degraded');
    assert.equal(payload.outbox_depth, 2);
    assert.equal(payload.outbox_oldest_age_seconds, 300);
    assert.equal(payload.unresolved_identity_count, 1);
    assert.equal(payload.permanent_failure_count, 1);
    assert.equal(payload.raw_capture_failure_count, 1);
    assert.equal(payload.purged_event_count, 2);
    for (const marker of RAW_MARKERS) {
      assert.equal(body.includes(marker), false, marker);
    }
    assert.equal(body.includes('private-contact-key'), false);
  });
});

test('connecting is degraded and disconnected or fatal is unavailable', async () => {
  const state = new HealthState({ spool: fakeSpool(), now: () => 2_000 });

  assert.equal((await state.snapshot()).status, 'degraded');
  state.setWhatsApp('disconnected');
  assert.equal((await state.snapshot()).status, 'unavailable');
  state.setWhatsApp('connected');
  state.setFatal(true);
  assert.equal((await state.snapshot()).status, 'unavailable');
});

test('malformed spool metrics fail controlled without exposing record content', async () => {
  const spool = {
    async list() {
      throw new Error(`malformed ${RAW_MARKERS.join(' ')}`);
    },
    async read() {
      throw new Error('must not be reached');
    },
  };
  const state = new HealthState({ spool, now: () => 2_000 });
  state.setWhatsApp('connected');
  const serialized = JSON.stringify(await state.snapshot());

  assert.equal(JSON.parse(serialized).status, 'unavailable');
  for (const marker of RAW_MARKERS) {
    assert.equal(serialized.includes(marker), false, marker);
  }
});

test('health server closes cleanly and rejects requests after shutdown', async () => {
  const state = new HealthState({ spool: fakeSpool(), now: () => 2_000 });
  const health = createHealthServer({
    host: '127.0.0.1',
    port: 0,
    snapshot: () => state.snapshot(),
  });
  await health.start();
  const address = health.address();
  await health.close();
  await health.close();
  await assert.rejects(fetch(`http://127.0.0.1:${address.port}/health`));
});
