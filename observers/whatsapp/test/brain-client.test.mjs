import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { BrainClient, BrainClientError } from '../src/brain-client.mjs';
import { TransportIds } from '../src/hmac.mjs';
import { normalizeInboundMessage } from '../src/normalize.mjs';
import { SafeSpool } from '../src/spool.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const IDS = new TransportIds(Buffer.from('t'.repeat(32), 'utf8'));
const TOKEN = 'observer-token-must-never-leak';
const RAW_PAYLOAD_MARKER = 'payload-marker-must-never-leak';

function safeEvent(messageId = 'brain-client-message') {
  return {
    ...normalizeInboundMessage(
      {
        key: {
          id: messageId,
          remoteJid: '15551234567@s.whatsapp.net',
          fromMe: false,
        },
        messageTimestamp: 2_000_000_000,
        pushName: RAW_PAYLOAD_MARKER,
        message: { conversation: 'synthetic body' },
      },
      2_000_000_001,
      IDS,
      'observer-client-test',
    ),
    observer_event_version: 2,
  };
}

function rawSafeEvent(messageId = 'brain-client-raw-message') {
  const event = safeEvent(messageId);
  return {
    ...event,
    external_ad_reply: {
      source_type: 'ad',
      source_id_present: true,
      source_id_length: 1,
      source_id_hmac: 'a'.repeat(64),
    },
    external_ad_reply_raw: { sourceType: 'ad', sourceId: 'raw-preserved' },
    transport_kind: 'ctwa_candidate',
  };
}

async function withServer(handler, callback) {
  const server = createServer(handler);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  const baseUrl = `http://127.0.0.1:${address.port}`;
  try {
    return await callback(baseUrl);
  } finally {
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
}

async function requestBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function jsonResponse(response, status, payload) {
  response.writeHead(status, { 'content-type': 'application/json' });
  response.end(JSON.stringify(payload));
}

async function expectClassification(promise, { retryable, status }) {
  await assert.rejects(promise, (error) => {
    assert.equal(error instanceof BrainClientError, true);
    assert.equal(error.retryable, retryable);
    assert.equal(error.status, status);
    assert.equal(error.message.includes(TOKEN), false);
    assert.equal(error.message.includes(RAW_PAYLOAD_MARKER), false);
    return true;
  });
}

test('POST uses exact route, bearer token, JSON content type, and exact safe body', async () => {
  const event = safeEvent();
  await withServer(async (request, response) => {
    const body = await requestBody(request);
    assert.equal(request.method, 'POST');
    assert.equal(request.url, '/internal/transport/events');
    assert.equal(request.headers.authorization, `Bearer ${TOKEN}`);
    assert.match(request.headers['content-type'], /^application\/json(?:;|$)/);
    assert.deepEqual(JSON.parse(body), event);
    for (const field of [
      'spool_version',
      'spooled_at',
      'display_name_expires_at',
      'filename',
      'attempt',
    ]) {
      assert.equal(Object.hasOwn(JSON.parse(body), field), false);
    }
    jsonResponse(response, 200, {
      status: 'ok',
      event_id: event.event_id,
      duplicate: false,
    });
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    assert.deepEqual(await client.ingest(event), {
      event_id: event.event_id,
      duplicate: false,
    });
  });
});

test('v2 raw attribution passes through byte-for-byte as validated', async () => {
  const event = rawSafeEvent();
  await withServer(async (request, response) => {
    const received = JSON.parse(await requestBody(request));
    assert.deepEqual(received.external_ad_reply_raw, event.external_ad_reply_raw);
    jsonResponse(response, 200, {
      status: 'ok',
      event_id: event.event_id,
      duplicate: false,
    });
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    await client.ingest(event);
  });
});

test('default request maximum accepts a raw event larger than the legacy limit', async () => {
  const event = rawSafeEvent('client-expanded-default');
  event.external_ad_reply_raw.unknownFutureField = 'x'.repeat(20_000);
  await withServer(async (request, response) => {
    const received = JSON.parse(await requestBody(request));
    assert.equal(received.external_ad_reply_raw.unknownFutureField.length, 20_000);
    jsonResponse(response, 200, {
      status: 'ok',
      event_id: event.event_id,
      duplicate: false,
    });
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    await client.ingest(event);
  });
});

test('request maximum is injectable and reports a non-sensitive error', async () => {
  const event = rawSafeEvent('client-over-limit');
  const client = new BrainClient({
    baseUrl: 'http://127.0.0.1:1',
    token: TOKEN,
    timeoutMs: 1_000,
    maxRequestBytes: 1,
  });
  await assert.rejects(client.ingest(event), (error) => {
    assert.equal(error instanceof TypeError, true);
    assert.match(error.message, /payload/i);
    assert.equal(error.message.includes('raw-preserved'), false);
    assert.equal(error.message.includes(TOKEN), false);
    return true;
  });
});

test('invalid raw attribution and missing v2 companions are rejected before HTTP', async () => {
  const event = rawSafeEvent('client-invalid-raw');
  let requests = 0;
  await withServer((_request, response) => {
    requests += 1;
    response.end();
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    const missingRaw = { ...event };
    delete missingRaw.external_ad_reply_raw;
    const malformedRaw = {
      ...event,
      external_ad_reply_raw: {
        $type: 'bytes',
        encoding: 'hex',
        data: '00',
      },
    };
    for (const invalid of [missingRaw, malformedRaw]) {
      await assert.rejects(client.ingest(invalid), /event payload/i);
    }
    assert.equal(requests, 0);
  });
});

test('legacy v1 events without a version or raw companion remain deliverable', async () => {
  const event = rawSafeEvent('client-legacy-v1');
  delete event.observer_event_version;
  delete event.external_ad_reply_raw;
  await withServer(async (request, response) => {
    assert.deepEqual(JSON.parse(await requestBody(request)), event);
    jsonResponse(response, 200, {
      status: 'ok',
      event_id: event.event_id,
      duplicate: false,
    });
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    await client.ingest(event);
  });
});

test('spool wrapper metadata is rejected locally and never transmitted', async () => {
  const event = safeEvent('wrapper-rejection');
  let requests = 0;
  await withServer((_request, response) => {
    requests += 1;
    response.end();
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    await assert.rejects(
      client.ingest({
        spool_version: 1,
        spooled_at: 2_000_000_002,
        display_name_expires_at: null,
        event,
      }),
      /event|payload/i,
    );
    assert.equal(requests, 0);
  });
});

test('raw or unknown event fields are rejected before any request', async () => {
  const event = safeEvent('raw-rejection');
  let requests = 0;
  await withServer((_request, response) => {
    requests += 1;
    response.end();
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    for (const unsafe of [
      { ...event, body: 'raw-body' },
      { ...event, remote_jid: 'raw-jid' },
      { ...event, unknown: true },
      {
        ...event,
        external_ad_reply: { ...event.external_ad_reply, sourceId: 'raw-id' },
      },
    ]) {
      await assert.rejects(client.ingest(unsafe), /event payload/i);
    }
    assert.equal(requests, 0);
  });
});

test('valid 200 duplicate response is returned as duplicate=true', async () => {
  const event = safeEvent('duplicate-response');
  await withServer((_request, response) => {
    jsonResponse(response, 200, {
      status: 'ok',
      event_id: event.event_id,
      duplicate: true,
    });
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    assert.deepEqual(await client.ingest(event), {
      event_id: event.event_id,
      duplicate: true,
    });
  });
});

test('success event_id mismatch and malformed success JSON fail closed', async () => {
  const event = safeEvent('protocol-failure');
  for (const mode of ['mismatch', 'malformed', 'shape']) {
    await withServer((_request, response) => {
      if (mode === 'malformed') {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end('{not-json');
      } else if (mode === 'mismatch') {
        jsonResponse(response, 200, {
          status: 'ok',
          event_id: IDS.eventId('observer-client-test', 'different'),
          duplicate: false,
        });
      } else {
        jsonResponse(response, 200, {
          status: 'ok',
          event_id: event.event_id,
          duplicate: 'false',
        });
      }
    }, async (baseUrl) => {
      const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
      await expectClassification(client.ingest(event), {
        retryable: false,
        status: 200,
      });
    });
  }
});

test('network failure is retryable and performs one bounded attempt', async () => {
  const event = safeEvent('network-failure');
  const client = new BrainClient({
    baseUrl: 'http://127.0.0.1:1',
    token: TOKEN,
    timeoutMs: 200,
  });
  await expectClassification(client.ingest(event), {
    retryable: true,
    status: null,
  });
});

test('timeout aborts the request and is retryable', async () => {
  const event = safeEvent('timeout');
  await withServer((_request, response) => {
    setTimeout(() => {
      if (!response.destroyed) {
        jsonResponse(response, 200, {
          status: 'ok',
          event_id: event.event_id,
          duplicate: false,
        });
      }
    }, 150);
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 20 });
    await expectClassification(client.ingest(event), {
      retryable: true,
      status: null,
    });
  });
});

test('timeout remains bounded while reading a stalled success body', async () => {
  const event = safeEvent('body-timeout');
  await withServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.write('{"status":"ok"');
    setTimeout(() => response.end('}'), 150);
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 20 });
    await expectClassification(client.ingest(event), {
      retryable: true,
      status: null,
    });
  });
});

test('500, 503 IDENTITY_UNAVAILABLE, 429, 408, and 425 are retryable', async () => {
  const event = safeEvent('retryable-status');
  for (const status of [500, 503, 429, 408, 425]) {
    await withServer((_request, response) => {
      jsonResponse(
        response,
        status,
        status === 503
          ? { error: 'IDENTITY_UNAVAILABLE' }
          : { error: 'TRANSIENT' },
      );
    }, async (baseUrl) => {
      const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
      await expectClassification(client.ingest(event), {
        retryable: true,
        status,
      });
    });
  }
});

test('400, 401, 403, and other non-retryable 4xx are permanent', async () => {
  const event = safeEvent('permanent-status');
  for (const status of [400, 401, 403, 404, 409, 422]) {
    await withServer((_request, response) => {
      jsonResponse(response, status, { error: 'PERMANENT' });
    }, async (baseUrl) => {
      const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
      await expectClassification(client.ingest(event), {
        retryable: false,
        status,
      });
    });
  }
});

test('an error never becomes success and token/payload never appear in errors', async () => {
  const event = safeEvent('privacy-error');
  await withServer((_request, response) => {
    response.writeHead(500, { 'content-type': 'text/plain' });
    response.end(`${TOKEN} ${RAW_PAYLOAD_MARKER}`);
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    await expectClassification(client.ingest(event), {
      retryable: true,
      status: 500,
    });
  });
});

test('timeout configuration is bounded and URL resolution cannot be changed by event data', async () => {
  for (const timeoutMs of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, 60_001]) {
    assert.throws(
      () =>
        new BrainClient({
          baseUrl: 'http://127.0.0.1:7777',
          token: TOKEN,
          timeoutMs,
        }),
      /timeout/i,
    );
  }
  assert.throws(
    () =>
      new BrainClient({
        baseUrl: 'https://user:password@127.0.0.1:7777/path?query=yes',
        token: TOKEN,
        timeoutMs: 1_000,
      }),
    /URL/i,
  );
  for (const maxRequestBytes of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(
      () =>
        new BrainClient({
          baseUrl: 'http://127.0.0.1:7777',
          token: TOKEN,
          timeoutMs: 1_000,
          maxRequestBytes,
        }),
      /request|payload|bytes/i,
    );
  }
});

test('ingest performs one attempt only and does not implement an internal retry loop', async () => {
  const event = safeEvent('single-attempt');
  let attempts = 0;
  await withServer((_request, response) => {
    attempts += 1;
    jsonResponse(response, 500, { error: 'TRANSIENT' });
  }, async (baseUrl) => {
    const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
    await expectClassification(client.ingest(event), {
      retryable: true,
      status: 500,
    });
    assert.equal(attempts, 1);
  });
});

test('client never acknowledges or deletes spool data on success or failure', async () => {
  const testRoot = await mkdtemp(path.join(HERE, '.client-spool-test-'));
  try {
    const spool = new SafeSpool(path.join(testRoot, 'outbox'), {
      now: () => 2_000_000_002,
    });
    const event = safeEvent('client-no-ack');
    await spool.put(event);

    await withServer((_request, response) => {
      jsonResponse(response, 500, { error: 'TRANSIENT' });
    }, async (baseUrl) => {
      const client = new BrainClient({ baseUrl, token: TOKEN, timeoutMs: 1_000 });
      await expectClassification(client.ingest(await spool.read(event.event_id)), {
        retryable: true,
        status: 500,
      });
    });
    assert.deepEqual(await spool.list(), [event.event_id]);
  } finally {
    await rm(testRoot, { recursive: true, force: true });
  }
});

test('all HTTP tests target local fake servers and never the production Brain endpoint', () => {
  assert.equal(withServer.toString().includes('127.0.0.1'), true);
});
