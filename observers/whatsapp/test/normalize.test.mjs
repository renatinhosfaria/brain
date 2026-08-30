import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { TransportIds } from '../src/hmac.mjs';
import { normalizeInboundMessage } from '../src/normalize.mjs';

const SECRET = Buffer.from('t'.repeat(32), 'utf8');
const IDS = new TransportIds(SECRET);
const CAPTURED_AT = 1_800_000_000.25;
const PHONE = '15551234567';
const RAW_JID = `${PHONE}@s.whatsapp.net`;
const RAW_MESSAGE_ID = 'synthetic-observer-message-001';
const RAW_BODY = 'Synthetic CTWA body 😀';
const RAW_SOURCE_ID = 'synthetic-source-id-123';
const RAW_CTWA_CLID = 'synthetic-ctwa-clid-456';
const RAW_SOURCE_URL =
  'https://ads.example.test/private/path?campaign=synthetic-secret-token';

function ctwaMessage(overrides = {}) {
  return {
    key: {
      id: RAW_MESSAGE_ID,
      remoteJid: RAW_JID,
      fromMe: false,
    },
    messageTimestamp: 1_799_999_999,
    pushName: '  Synthetic\nName\u0000  ',
    message: {
      extendedTextMessage: {
        text: RAW_BODY,
        contextInfo: {
          externalAdReply: {
            sourceType: 'ad',
            sourceApp: 'instagram',
            sourceId: RAW_SOURCE_ID,
            sourceUrl: RAW_SOURCE_URL,
            ctwaClid: RAW_CTWA_CLID,
            showAdAttribution: false,
            clickToWhatsappCall: true,
            containsAutoReply: false,
            thumbnail: Buffer.from('raw-thumbnail'),
            arbitraryRawField: 'must-not-leak',
          },
          arbitraryContext: 'raw-context-must-not-leak',
        },
      },
      arbitraryMessageTree: { secret: 'arbitrary-tree-must-not-leak' },
    },
    ...overrides,
  };
}

test('historical CTWA normalizes to the exact Brain-safe envelope', () => {
  const safe = normalizeInboundMessage(
    ctwaMessage(),
    CAPTURED_AT,
    IDS,
    'observer-test-device',
  );

  assert.deepEqual(safe, {
    event_id: IDS.eventId('observer-test-device', RAW_MESSAGE_ID),
    observer_device_id: 'observer-test-device',
    received_at: CAPTURED_AT,
    message_timestamp: 1_799_999_999,
    remote_jid_hmac: IDS.jidHmac(RAW_JID),
    contact_key: IDS.contactKey(PHONE),
    body_hmac: IDS.bodyHmac(RAW_BODY),
    body_length: Array.from(RAW_BODY).length,
    display_name: '  SyntheticName  ',
    native_type: 'extendedTextMessage',
    transport_kind: 'ctwa_candidate',
    external_ad_reply: {
      source_type: 'ad',
      source_app: 'instagram',
      source_id_present: true,
      source_id_length: Array.from(RAW_SOURCE_ID).length,
      source_id_hmac: IDS.opaqueHmac(RAW_SOURCE_ID),
      source_url_hostname: 'ads.example.test',
      source_url_length: Array.from(RAW_SOURCE_URL).length,
      source_url_hmac: IDS.opaqueHmac(RAW_SOURCE_URL),
      ctwa_clid_present: true,
      ctwa_clid_length: Array.from(RAW_CTWA_CLID).length,
      ctwa_clid_hmac: IDS.opaqueHmac(RAW_CTWA_CLID),
      show_ad_attribution: false,
      click_to_whatsapp_call: true,
      contains_auto_reply: false,
    },
  });
});

test('ordinary conversation and extended text use only the two proven paths', () => {
  const conversation = ctwaMessage({
    key: { id: 'ordinary-1', remoteJid: RAW_JID, fromMe: false },
    pushName: undefined,
    message: { conversation: 'ordinary text' },
  });
  const extended = ctwaMessage({
    key: { id: 'ordinary-2', remoteJid: RAW_JID, fromMe: false },
    pushName: '',
    message: { extendedTextMessage: { text: 'extended ordinary text' } },
  });

  const first = normalizeInboundMessage(
    conversation,
    CAPTURED_AT,
    IDS,
    'observer-a',
  );
  const second = normalizeInboundMessage(
    extended,
    CAPTURED_AT,
    IDS,
    'observer-a',
  );

  assert.equal(first.native_type, 'conversation');
  assert.equal(first.transport_kind, 'ordinary_inbound');
  assert.equal('external_ad_reply' in first, false);
  assert.equal('display_name' in first, false);
  assert.equal(second.native_type, 'extendedTextMessage');
  assert.equal(second.transport_kind, 'ordinary_inbound');
});

test('self, group, missing text, and unsupported messages return null', () => {
  const self = ctwaMessage({
    key: { id: 'self', remoteJid: RAW_JID, fromMe: true },
  });
  const group = ctwaMessage({
    key: { id: 'group', remoteJid: '123456789@g.us', fromMe: false },
  });
  const missing = ctwaMessage({ message: {} });
  const unsupported = ctwaMessage({ message: { imageMessage: { caption: 'raw' } } });

  for (const raw of [self, group, missing, unsupported]) {
    assert.equal(
      normalizeInboundMessage(raw, CAPTURED_AT, IDS, 'observer-a'),
      null,
    );
  }
});

test('event identity is derived but raw message ID is never returned', () => {
  const safe = normalizeInboundMessage(
    ctwaMessage(),
    CAPTURED_AT,
    IDS,
    'observer-test-device',
  );

  assert.equal(safe.event_id, IDS.eventId('observer-test-device', RAW_MESSAGE_ID));
  assert.equal('message_id' in safe, false);
  assert.equal('id' in safe, false);
});

test('direct numeric PN derives contact_key while an unresolved LID does not', () => {
  const direct = normalizeInboundMessage(
    ctwaMessage(),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );
  const lidJid = '123456789012345@lid';
  const lid = normalizeInboundMessage(
    ctwaMessage({
      key: { id: 'lid-message', remoteJid: lidJid, fromMe: false },
    }),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );

  assert.equal(direct.contact_key, IDS.contactKey(PHONE));
  assert.equal(direct.remote_jid_hmac, IDS.jidHmac(RAW_JID));
  assert.equal(lid.remote_jid_hmac, IDS.jidHmac(lidJid));
  assert.equal('contact_key' in lid, false);
});

test('body length uses Unicode code points and preserves exact UTF-8 body HMAC', () => {
  const emoji = normalizeInboundMessage(
    ctwaMessage({
      key: { id: 'emoji', remoteJid: RAW_JID, fromMe: false },
      message: { conversation: '😀' },
    }),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );
  const mixedBody = 'common + 😀 + text';
  const mixed = normalizeInboundMessage(
    ctwaMessage({
      key: { id: 'mixed', remoteJid: RAW_JID, fromMe: false },
      message: { conversation: mixedBody },
    }),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );

  assert.equal(emoji.body_length, 1);
  assert.equal(emoji.body_hmac, IDS.bodyHmac('😀'));
  assert.equal(mixed.body_length, Array.from(mixedBody).length);
  assert.equal(mixed.body_hmac, IDS.bodyHmac(mixedBody));
});

test('CTWA detector does not require showAdAttribution or infer human semantics', () => {
  const safe = normalizeInboundMessage(
    ctwaMessage(),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );
  const noStrongSignal = ctwaMessage();
  noStrongSignal.message.extendedTextMessage.contextInfo.externalAdReply = {
    sourceType: 'ad',
    showAdAttribution: true,
    containsAutoReply: false,
  };
  const ordinary = normalizeInboundMessage(
    noStrongSignal,
    CAPTURED_AT,
    IDS,
    'observer-a',
  );

  assert.equal(safe.transport_kind, 'ctwa_candidate');
  assert.equal(safe.external_ad_reply.show_ad_attribution, false);
  assert.equal(safe.external_ad_reply.contains_auto_reply, false);
  assert.equal(ordinary.transport_kind, 'ordinary_inbound');
  for (const forbidden of [
    'ctwa_first_contact',
    'human_inbound',
    'ctwa_attributed_inbound',
    'inbound_kind',
  ]) {
    assert.equal(JSON.stringify(safe).includes(forbidden), false);
  }
});

test('display name is control-stripped and bounded to 160 code points', () => {
  const longName = `A\n\u0000${'😀'.repeat(170)}`;
  const safe = normalizeInboundMessage(
    ctwaMessage({ pushName: longName }),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );

  assert.equal(Array.from(safe.display_name).length, 160);
  assert.equal(safe.display_name.includes('\n'), false);
  assert.equal(safe.display_name.includes('\u0000'), false);
  assert.equal('pushName' in safe, false);
});

test('serialized safe event contains no raw payload or arbitrary Baileys tree', () => {
  const safe = normalizeInboundMessage(
    ctwaMessage(),
    CAPTURED_AT,
    IDS,
    'observer-a',
  );
  const serialized = JSON.stringify(safe);

  for (const raw of [
    RAW_BODY,
    RAW_JID,
    RAW_MESSAGE_ID,
    RAW_SOURCE_ID,
    RAW_CTWA_CLID,
    RAW_SOURCE_URL,
    'raw-context-must-not-leak',
    'arbitrary-tree-must-not-leak',
    'must-not-leak',
    'raw-thumbnail',
    'contextInfo',
    'externalAdReply',
    'sourceId',
    'ctwaClid',
    'sourceUrl',
  ]) {
    assert.equal(serialized.includes(raw), false, raw);
  }
  assert.equal(safe.external_ad_reply.source_url_hostname, 'ads.example.test');
});

test('production observer source contains no explicit forbidden mutation calls', async () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const sourceDir = path.resolve(here, '../src');
  const files = (await readdir(sourceDir)).filter((name) => name.endsWith('.mjs'));
  const forbiddenCall = /\.\s*(?:sendMessage|readMessages|sendPresenceUpdate|sendReaction|groupParticipantsUpdate|groupSettingUpdate|groupCreate|groupLeave|groupUpdateSubject|groupUpdateDescription|updateProfilePicture|updateProfileName|updateBlockStatus)\s*\(/;

  for (const file of files) {
    const source = await readFile(path.join(sourceDir, file), 'utf8');
    assert.doesNotMatch(source, forbiddenCall, file);
  }
});

test('logFailure emits only bounded technical fields', async () => {
  const { logFailure } = await import('../src/main.mjs');
  const written = [];
  const original = process.stderr.write;
  process.stderr.write = (chunk) => {
    written.push(String(chunk));
    return true;
  };
  try {
    logFailure('upsert_processing_failed', new TypeError('boom 5534999772714'));
  } finally {
    process.stderr.write = original;
  }
  const record = JSON.parse(written.join(''));
  assert.deepEqual(Object.keys(record).sort(), [
    'component',
    'error_message',
    'error_name',
    'reason',
  ]);
  assert.equal(record.error_name, 'TypeError');
  assert.equal(record.reason, 'upsert_processing_failed');
  assert.ok(record.error_message.length <= 200);
});
