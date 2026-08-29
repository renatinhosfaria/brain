import assert from 'node:assert/strict';
import test from 'node:test';

import { TransportIds } from '../src/hmac.mjs';

const SECRET = Buffer.from('t'.repeat(32), 'utf8');

const GOLDEN = Object.freeze({
  event: 'waevt_b6cd393ade50414e98138b300f8f1dd90a0a30dec0cd0d1c65be7243f34b665c',
  contact: 'b54287000ef28fc2c7d27b7dcbb8b4d2d839a5540d337fc81e607b7f6ca19a27',
  body: 'c140814c5efd1dff3069166bb1132b138bfe9c6d769d35313ff68a5c0133e85d',
  jid: '084fa55eb1805dbfe59f1444f0f9d10973bebb4e2d4a8f728065803663ed1361',
  opaque: '479b1e4931315b3814e746c0b1acc4f79f1a4e5d49fec5f1f19245fca1e4c022',
  unicode: 'ab3f31fcbe7302882561d2a4474fe4c9ac22201ed4526dd3895770a5adca6355',
});

test('rejects a transport secret shorter than 32 bytes', () => {
  assert.throws(() => new TransportIds(Buffer.from('short')), /32 bytes/);
});

test('eventId is deterministic, prefixed, and matches Python RuntimeIds', () => {
  const first = new TransportIds(SECRET);
  const second = new TransportIds(Buffer.from(SECRET));

  assert.equal(
    first.eventId('observer-test-device', 'synthetic-message-id-001'),
    GOLDEN.event,
  );
  assert.equal(
    second.eventId('observer-test-device', 'synthetic-message-id-001'),
    GOLDEN.event,
  );
  assert.match(GOLDEN.event, /^waevt_[0-9a-f]{64}$/);
});

test('eventId changes with observer device and message identity', () => {
  const ids = new TransportIds(SECRET);
  const baseline = ids.eventId('observer-a', 'message-a');

  assert.notEqual(baseline, ids.eventId('observer-b', 'message-a'));
  assert.notEqual(baseline, ids.eventId('observer-a', 'message-b'));
});

test('event framing is structurally unambiguous', () => {
  const ids = new TransportIds(SECRET);

  assert.notEqual(ids.eventId('ab', 'c'), ids.eventId('a', 'bc'));
});

test('contactKey matches Python and validates canonical phones', () => {
  const ids = new TransportIds(SECRET);

  assert.equal(ids.contactKey('15551234567'), GOLDEN.contact);
  for (const invalid of ['', '+15551234567', '05551234567', 'not-a-phone']) {
    assert.throws(() => ids.contactKey(invalid), /canonical/i);
  }
});

test('bodyHmac, jidHmac, and opaqueHmac match Python golden vectors', () => {
  const ids = new TransportIds(SECRET);

  assert.equal(ids.bodyHmac('Synthetic body 😀 exact'), GOLDEN.body);
  assert.equal(ids.jidHmac('15551234567@s.whatsapp.net'), GOLDEN.jid);
  assert.equal(ids.opaqueHmac('synthetic-source-value'), GOLDEN.opaque);
});

test('transport domains remain distinct for identical bytes', () => {
  const ids = new TransportIds(SECRET);
  const value = '15551234567';
  const tags = new Set([
    ids.contactKey(value),
    ids.bodyHmac(value),
    ids.jidHmac(value),
    ids.opaqueHmac(value),
  ]);

  assert.equal(tags.size, 4);
});

test('UTF-8 text is exact and matches Python without Unicode normalization', () => {
  const ids = new TransportIds(SECRET);

  assert.equal(ids.bodyHmac('ação 😀'), GOLDEN.unicode);
  assert.notEqual(ids.bodyHmac('ação 😀'), ids.bodyHmac('ação 😀'));
});

test('observer API requires only one transport secret and does not expose it', () => {
  const ids = new TransportIds(SECRET);

  assert.equal(TransportIds.length, 1);
  assert.deepEqual(Object.keys(ids), []);
  assert.equal('runtimeSecret' in ids, false);
});
