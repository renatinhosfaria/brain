import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  symlink,
  writeFile,
} from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { TransportIds } from '../src/hmac.mjs';
import { normalizeInboundMessage } from '../src/normalize.mjs';
import { SafeSpool } from '../src/spool.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const IDS = new TransportIds(Buffer.from('t'.repeat(32), 'utf8'));
const CAPTURED_AT = 2_000_000_000;
const RAW_PHONE = '15551234567';
const RAW_JID = `${RAW_PHONE}@s.whatsapp.net`;
const RAW_LID = '123456789012345@lid';
const RAW_MESSAGE_ID = 'raw-observer-message-unique-001';
const RAW_BODY = 'raw-body-unique-😀-secret';
const RAW_SOURCE_ID = 'raw-source-id-unique-002';
const RAW_CTWA_CLID = 'raw-ctwa-clid-unique-003';
const RAW_SOURCE_URL =
  'https://ads.example.test/private/raw-url-unique-004?secret=yes';
const RAW_PUSH_NAME = 'Raw\nPush\u0000Name';

function rawCtwa(overrides = {}) {
  return {
    key: {
      id: RAW_MESSAGE_ID,
      remoteJid: RAW_LID,
      remoteJidAlt: RAW_JID,
      fromMe: false,
    },
    messageTimestamp: CAPTURED_AT - 1,
    pushName: RAW_PUSH_NAME,
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
            showAdAttribution: true,
            clickToWhatsappCall: true,
            containsAutoReply: false,
            thumbnail: Buffer.from('raw-thumbnail-unique'),
          },
          rawContext: 'raw-context-tree-unique',
        },
      },
    },
    ...overrides,
  };
}

function safeEvent(messageId = RAW_MESSAGE_ID, capturedAt = CAPTURED_AT) {
  const raw = rawCtwa({
    key: {
      id: messageId,
      remoteJid: RAW_LID,
      remoteJidAlt: RAW_JID,
      fromMe: false,
    },
  });
  return {
    ...normalizeInboundMessage(raw, capturedAt, IDS, 'observer-spool-test'),
    observer_event_version: 2,
  };
}

async function withSpool(
  callback,
  initialNow = CAPTURED_AT + 10,
  spoolOptions = {},
) {
  const testRoot = await mkdtemp(path.join(HERE, '.spool-test-'));
  const rootDir = path.join(testRoot, 'outbox');
  let now = initialNow;
  const spool = new SafeSpool(rootDir, { ...spoolOptions, now: () => now });
  try {
    return await callback({
      rootDir,
      setNow(value) {
        now = value;
      },
      spool,
      testRoot,
    });
  } finally {
    await rm(testRoot, { recursive: true, force: true });
  }
}

async function withSynchronizedPublications(count, callback) {
  const originalLink = fs.promises.link;
  const originalRename = fs.promises.rename;
  const pending = [];
  const milestones = Array.from({ length: count }, () => {
    const milestone = {};
    milestone.promise = new Promise((resolve, reject) => {
      milestone.resolve = resolve;
      milestone.reject = reject;
    });
    return milestone;
  });

  function intercept(original) {
    return async (source, target) => {
      if (path.basename(source).endsWith('.tmp') && target.endsWith('.json')) {
        return new Promise((resolve, reject) => {
          pending.push({ original, reject, resolve, source, target });
          milestones[pending.length - 1]?.resolve();
        });
      }
      return original(source, target);
    };
  }

  fs.promises.link = intercept(originalLink);
  fs.promises.rename = intercept(originalRename);
  syncBuiltinESMExports();
  try {
    await callback({
      async release() {
        await milestones[count - 1].promise;
        for (const operation of pending) {
          try {
            await operation.original(operation.source, operation.target);
            operation.resolve();
          } catch (error) {
            operation.reject(error);
          }
        }
      },
      waitForPublications(number) {
        return milestones[number - 1].promise;
      },
    });
  } finally {
    for (const milestone of milestones) {
      milestone.reject(new Error('publication synchronization ended'));
    }
    for (const operation of pending) {
      operation.reject(new Error('publication synchronization ended'));
    }
    fs.promises.link = originalLink;
    fs.promises.rename = originalRename;
    syncBuiltinESMExports();
  }
}

test('root is 0700 and an event is atomically installed as event_id.json mode 0600', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent();
    const result = await spool.put(event);
    const entries = await readdir(rootDir);
    const target = path.join(rootDir, `${event.event_id}.json`);

    assert.deepEqual(result, { event_id: event.event_id, duplicate: false });
    assert.equal((await lstat(rootDir)).mode & 0o777, 0o700);
    assert.equal((await lstat(target)).mode & 0o777, 0o600);
    assert.deepEqual(entries, [`${event.event_id}.json`]);
    assert.equal(entries[0].includes(RAW_MESSAGE_ID), false);
    assert.equal(entries.some((name) => name.endsWith('.tmp')), false);
  });
});

test('new spool records are v2 and retain raw attribution', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent('raw-v2');
    await spool.put(event);
    const filename = path.join(rootDir, `${event.event_id}.json`);
    const wrapper = JSON.parse(await readFile(filename, 'utf8'));

    assert.equal(wrapper.spool_version, 2);
    assert.deepEqual(wrapper.event.external_ad_reply_raw, event.external_ad_reply_raw);
    assert.deepEqual(await spool.read(event.event_id), event);
  });
});

test('valid v1 spool records without raw attribution remain readable', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent('legacy-v1');
    delete event.observer_event_version;
    delete event.external_ad_reply_raw;
    const wrapper = {
      spool_version: 1,
      spooled_at: CAPTURED_AT + 10,
      display_name_expires_at: event.received_at + 24 * 60 * 60,
      event,
    };
    await mkdir(rootDir, { recursive: true, mode: 0o700 });
    await writeFile(path.join(rootDir, `${event.event_id}.json`), JSON.stringify(wrapper), {
      mode: 0o600,
    });

    assert.deepEqual(await spool.read(event.event_id), event);

    wrapper.event = {
      ...event,
      observer_event_version: 2,
      external_ad_reply_raw: safeEvent('legacy-v1').external_ad_reply_raw,
    };
    await writeFile(
      path.join(rootDir, `${event.event_id}.json`),
      JSON.stringify(wrapper),
      { mode: 0o600 },
    );
    await assert.rejects(spool.read(event.event_id), /record|event/i);
  });
});

test('quarantine is private, conflict-safe, and purged with events', async () => {
  await withSpool(async ({ rootDir, spool, setNow }) => {
    const event = safeEvent('quarantine');
    await spool.quarantine({
      event_id: event.event_id,
      captured_at: CAPTURED_AT,
      reason: 'raw_depth',
      external_ad_reply_raw: event.external_ad_reply_raw,
    });
    const quarantineDir = path.join(rootDir, 'quarantine');
    const target = path.join(quarantineDir, `${event.event_id}.json`);
    const record = JSON.parse(await readFile(target, 'utf8'));

    assert.equal((await lstat(quarantineDir)).mode & 0o777, 0o700);
    assert.equal((await lstat(target)).mode & 0o777, 0o600);
    assert.deepEqual(record, {
      quarantine_version: 1,
      event_id: event.event_id,
      captured_at: CAPTURED_AT,
      reason: 'raw_depth',
      external_ad_reply_raw: event.external_ad_reply_raw,
    });
    await assert.rejects(
      spool.quarantine({ ...record, reason: 'raw_size' }),
      /conflict/i,
    );

    setNow(CAPTURED_AT + 72 * 60 * 60 + 1);
    assert.deepEqual(await spool.purgeOlderThan(CAPTURED_AT + 1), {
      events: 0,
      quarantine: 1,
    });
    assert.deepEqual(await readdir(quarantineDir), []);
  });
});

test('quarantine publication is atomic and leaves no temporary file', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent('quarantine-atomic');
    const quarantineDir = path.join(rootDir, 'quarantine');
    const target = path.join(quarantineDir, `${event.event_id}.json`);

    await withSynchronizedPublications(1, async ({ release, waitForPublications }) => {
      const publication = spool.quarantine({
        event_id: event.event_id,
        captured_at: CAPTURED_AT,
        reason: 'raw_size',
        external_ad_reply_raw: event.external_ad_reply_raw,
      });
      await waitForPublications(1);
      await assert.rejects(readFile(target), (error) => error?.code === 'ENOENT');
      await release();
      assert.deepEqual(await publication, {
        event_id: event.event_id,
        duplicate: false,
      });
    });

    assert.deepEqual(await readdir(quarantineDir), [`${event.event_id}.json`]);
  });
});

test('quarantine enforces its complete record byte limit', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent('quarantine-limit');
    await assert.rejects(
      spool.quarantine({
        event_id: event.event_id,
        captured_at: CAPTURED_AT,
        reason: 'raw_size',
        external_ad_reply_raw: { sourceId: 'x'.repeat(180) },
      }),
      /quarantine record/i,
    );
    assert.deepEqual(await readdir(rootDir).catch(() => []), []);
  }, CAPTURED_AT + 10, {
    rawLimits: { maxBytes: 32, maxDepth: 32, maxNodes: 10_000 },
    quarantineMaxBytes: 256,
  });
});

test('quarantine rejects a symlink directory and symlink record target', async () => {
  await withSpool(async ({ rootDir, spool, testRoot }) => {
    await spool.list();
    const outsideDir = path.join(testRoot, 'outside-quarantine');
    await mkdir(outsideDir, { mode: 0o700 });
    await symlink(outsideDir, path.join(rootDir, 'quarantine'));

    const first = safeEvent('quarantine-directory-link');
    await assert.rejects(
      spool.quarantine({
        event_id: first.event_id,
        captured_at: CAPTURED_AT,
        reason: 'raw_type',
      }),
      /quarantine|symlink/i,
    );
    assert.deepEqual(await readdir(outsideDir), []);
  });

  await withSpool(async ({ rootDir, spool, testRoot }) => {
    await spool.purgeOlderThan(CAPTURED_AT);
    const outside = path.join(testRoot, 'outside-record.json');
    await writeFile(outside, 'outside-stays');
    const event = safeEvent('quarantine-record-link');
    const target = path.join(rootDir, 'quarantine', `${event.event_id}.json`);
    await symlink(outside, target);

    await assert.rejects(
      spool.quarantine({
        event_id: event.event_id,
        captured_at: CAPTURED_AT,
        reason: 'raw_type',
      }),
      /conflict|regular file|symlink/i,
    );
    assert.equal(await readFile(outside, 'utf8'), 'outside-stays');
  });
});

test('privacy fixture persists only the requested raw attribution subtree', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent();
    await spool.put(event);
    const allBytes = [];
    for (const entry of await readdir(rootDir)) {
      const entryPath = path.join(rootDir, entry);
      if ((await lstat(entryPath)).isFile()) {
        allBytes.push(await readFile(entryPath));
      }
    }
    const serialized = Buffer.concat(allBytes).toString('utf8');

    for (const raw of [
      RAW_BODY,
      RAW_JID,
      RAW_LID,
      RAW_PHONE,
      RAW_MESSAGE_ID,
      RAW_PUSH_NAME,
      'raw-context-tree-unique',
    ]) {
      assert.equal(serialized.includes(raw), false, raw);
    }
    for (const retained of [
      RAW_SOURCE_ID,
      RAW_CTWA_CLID,
      RAW_SOURCE_URL,
      'cmF3LXRodW1ibmFpbC11bmlxdWU=',
    ]) {
      assert.equal(serialized.includes(retained), true, retained);
    }
    assert.equal(serialized.includes('RawPushName'), true);
  });
});

test('v2 events reject missing companions and invalid encoded raw attribution', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent('v2-companions');
    const withoutRaw = { ...event };
    delete withoutRaw.external_ad_reply_raw;
    const withoutNormalized = { ...event };
    delete withoutNormalized.external_ad_reply;

    for (const invalid of [
      withoutRaw,
      withoutNormalized,
      {
        ...event,
        external_ad_reply_raw: {
          $type: 'bytes',
          encoding: 'hex',
          data: '00',
        },
      },
    ]) {
      await assert.rejects(spool.put(invalid), /safe event/i);
    }
    await assert.rejects(lstat(rootDir), (error) => error?.code === 'ENOENT');

    const ordinaryWithExternal = {
      ...event,
      transport_kind: 'ordinary_inbound',
      external_ad_reply: {
        source_type: 'catalog',
        source_id_present: true,
        source_id_length: 1,
        source_id_hmac: 'a'.repeat(64),
      },
    };
    assert.deepEqual(await spool.put(ordinaryWithExternal), {
      event_id: event.event_id,
      duplicate: false,
    });
  });
});

test('strict allowlist accepts the normalized event and rejects raw or unknown fields', async () => {
  await withSpool(async ({ spool }) => {
    const event = safeEvent();
    await spool.put(event);

    for (const field of [
      'body',
      'text',
      'message',
      'raw_message',
      'remote_jid',
      'jid',
      'lid',
      'phone',
      'message_id',
      'observer_message_id',
      'payload',
      'raw',
      'thumbnail',
      'joined_body',
      'unknown',
    ]) {
      await assert.rejects(
        spool.put({ ...event, event_id: IDS.eventId('observer-spool-test', field), [field]: 'raw-value' }),
        /safe event/i,
      );
    }

    for (const field of ['sourceId', 'ctwaClid', 'sourceUrl', 'contextInfo']) {
      await assert.rejects(
        spool.put({
          ...event,
          event_id: IDS.eventId('observer-spool-test', `nested-${field}`),
          external_ad_reply: { ...event.external_ad_reply, [field]: 'raw-value' },
        }),
        /safe event/i,
      );
    }
  });
});

test('strict allowlist mirrors Brain optional-null semantics', async () => {
  await withSpool(async ({ spool }) => {
    const event = safeEvent('brain-null-optionals');
    const compatible = {
      ...event,
      message_timestamp: null,
      contact_key: null,
      display_name: null,
      transport_kind: 'ordinary_inbound',
      external_ad_reply: {
        source_type: null,
        source_app: null,
        source_id_present: false,
        source_id_length: null,
        source_id_hmac: null,
        source_url_hostname: null,
        source_url_length: null,
        source_url_hmac: null,
        ctwa_clid_present: false,
        ctwa_clid_length: null,
        ctwa_clid_hmac: null,
      },
    };

    await spool.put(compatible);
    assert.deepEqual(await spool.read(compatible.event_id), compatible);
  });
});

test('identical put is idempotent without renewing spool or display-name age', async () => {
  await withSpool(async ({ rootDir, setNow, spool }) => {
    const event = safeEvent();
    await spool.put(event);
    const target = path.join(rootDir, `${event.event_id}.json`);
    const first = JSON.parse(await readFile(target, 'utf8'));

    setNow(CAPTURED_AT + 20_000);
    assert.deepEqual(await spool.put(event), {
      event_id: event.event_id,
      duplicate: true,
    });
    const replay = JSON.parse(await readFile(target, 'utf8'));

    assert.equal(replay.spooled_at, first.spooled_at);
    assert.equal(replay.display_name_expires_at, first.display_name_expires_at);
    assert.deepEqual(replay, first);
  });
});

test('same event_id with conflicting safe payload fails closed and preserves original', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent();
    await spool.put(event);
    const target = path.join(rootDir, `${event.event_id}.json`);
    const before = await readFile(target, 'utf8');
    const conflict = { ...event, body_hmac: 'f'.repeat(64) };

    await assert.rejects(spool.put(conflict), /conflict/i);
    assert.equal(await readFile(target, 'utf8'), before);
  });
});

test('concurrent identical puts install once without renewing winner metadata', async () => {
  await withSpool(async ({ rootDir, setNow, spool }) => {
    const event = safeEvent('concurrent-identical');
    const target = path.join(rootDir, `${event.event_id}.json`);
    const firstNow = CAPTURED_AT + 10;
    const secondNow = CAPTURED_AT + 20_000;

    await withSynchronizedPublications(2, async ({ release, waitForPublications }) => {
      const first = spool.put(event);
      await waitForPublications(1);
      setNow(secondNow);
      const second = spool.put(event);
      await waitForPublications(2);
      await release();
      const results = await Promise.all([first, second]);

      assert.deepEqual(
        results.map((result) => result.duplicate).sort(),
        [false, true],
      );
    });

    const wrapper = JSON.parse(await readFile(target, 'utf8'));
    assert.equal(wrapper.spooled_at, firstNow);
    assert.equal(
      wrapper.display_name_expires_at,
      event.received_at + 24 * 60 * 60,
    );
    assert.deepEqual(wrapper.event, event);
    assert.deepEqual(await readdir(rootDir), [`${event.event_id}.json`]);
  });
});

test('concurrent conflicting puts produce one winner and one conflict without overwrite', async () => {
  await withSpool(async ({ rootDir, setNow, spool }) => {
    const original = safeEvent('concurrent-conflict');
    const conflicting = { ...original, body_hmac: 'f'.repeat(64) };
    const target = path.join(rootDir, `${original.event_id}.json`);
    const firstNow = CAPTURED_AT + 10;
    const secondNow = CAPTURED_AT + 20_000;

    let results;
    await withSynchronizedPublications(2, async ({ release, waitForPublications }) => {
      const first = spool.put(original);
      await waitForPublications(1);
      setNow(secondNow);
      const second = spool.put(conflicting);
      await waitForPublications(2);
      await release();
      results = await Promise.allSettled([first, second]);
    });

    assert.equal(results.filter((result) => result.status === 'fulfilled').length, 1);
    assert.equal(results.filter((result) => result.status === 'rejected').length, 1);
    assert.match(
      results.find((result) => result.status === 'rejected').reason.message,
      /conflict/i,
    );

    const wrapper = JSON.parse(await readFile(target, 'utf8'));
    const winnerIndex = results.findIndex((result) => result.status === 'fulfilled');
    const expectedEvent = [original, conflicting][winnerIndex];
    const expectedSpooledAt = [firstNow, secondNow][winnerIndex];
    assert.deepEqual(wrapper.event, expectedEvent);
    assert.equal(wrapper.spooled_at, expectedSpooledAt);
    assert.equal(
      wrapper.display_name_expires_at,
      expectedEvent.received_at + 24 * 60 * 60,
    );
    assert.deepEqual(await readdir(rootDir), [`${original.event_id}.json`]);
  });
});

test('concurrent symlink target is never replaced or followed', async () => {
  await withSpool(async ({ rootDir, spool, testRoot }) => {
    const event = safeEvent('concurrent-symlink');
    const target = path.join(rootDir, `${event.event_id}.json`);
    const outside = path.join(testRoot, 'outside-concurrent.json');
    await writeFile(outside, 'outside-stays');

    await withSynchronizedPublications(1, async ({ release, waitForPublications }) => {
      const publication = spool.put(event);
      await waitForPublications(1);
      await symlink(outside, target);
      await release();
      await assert.rejects(publication, /conflict|regular file|symlink/i);
    });

    assert.equal((await lstat(target)).isSymbolicLink(), true);
    assert.equal(await readFile(outside, 'utf8'), 'outside-stays');
    assert.deepEqual(
      (await readdir(rootDir)).filter((entry) => entry.endsWith('.tmp')),
      [],
    );
  });
});

test('stress: many identical concurrent puts install exactly once', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent('stress-identical');
    const results = await Promise.all(
      Array.from({ length: 16 }, () => spool.put(event)),
    );

    assert.equal(results.filter((result) => result.duplicate === false).length, 1);
    assert.equal(results.filter((result) => result.duplicate === true).length, 15);
    assert.deepEqual(await spool.read(event.event_id), event);
    assert.deepEqual(await readdir(rootDir), [`${event.event_id}.json`]);
  });
});

test('stress: many conflicting concurrent puts never overwrite the winner', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const base = safeEvent('stress-conflicts');
    const candidates = Array.from({ length: 16 }, (_, index) => ({
      ...base,
      body_hmac: index.toString(16).padStart(64, '0'),
    }));
    const results = await Promise.allSettled(
      candidates.map((event) => spool.put(event)),
    );

    assert.equal(results.filter((result) => result.status === 'fulfilled').length, 1);
    assert.equal(results.filter((result) => result.status === 'rejected').length, 15);
    for (const result of results.filter((entry) => entry.status === 'rejected')) {
      assert.match(result.reason.message, /conflict/i);
    }
    const winnerIndex = results.findIndex((result) => result.status === 'fulfilled');
    assert.deepEqual(await spool.read(base.event_id), candidates[winnerIndex]);
    assert.deepEqual(await readdir(rootDir), [`${base.event_id}.json`]);
  });
});

test('list is deterministic, ignores temp/arbitrary files, and never follows symlinks', async () => {
  await withSpool(async ({ rootDir, spool, testRoot }) => {
    const second = safeEvent('message-b');
    const first = safeEvent('message-a');
    await spool.put(second);
    await spool.put(first);
    await writeFile(path.join(rootDir, '.safe-spool-leftover.tmp'), 'ignored');
    await writeFile(path.join(rootDir, 'arbitrary.json'), 'ignored');
    const outside = path.join(testRoot, 'outside.json');
    const linkedId = IDS.eventId('observer-spool-test', 'linked');
    await writeFile(outside, JSON.stringify({ secret: 'outside' }));
    await symlink(outside, path.join(rootDir, `${linkedId}.json`));

    assert.deepEqual(await spool.list(), [first.event_id, second.event_id].sort());
    await assert.rejects(spool.read(linkedId), /regular file|symlink/i);
    assert.equal(await readFile(outside, 'utf8'), JSON.stringify({ secret: 'outside' }));
  });
});

test('read validates the wrapper again and returns only the exact Brain event', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    const event = safeEvent();
    await spool.put(event);
    const returned = await spool.read(event.event_id);

    assert.deepEqual(returned, event);
    assert.equal('spool_version' in returned, false);
    assert.equal('spooled_at' in returned, false);
    assert.equal('display_name_expires_at' in returned, false);

    const target = path.join(rootDir, `${event.event_id}.json`);
    await writeFile(target, '{malformed', { mode: 0o600 });
    await assert.rejects(spool.read(event.event_id), /record|JSON/i);
  });
});

test('ack removes only the exact event and is idempotent', async () => {
  await withSpool(async ({ spool }) => {
    const first = safeEvent('ack-a');
    const second = safeEvent('ack-b');
    await spool.put(first);
    await spool.put(second);

    assert.equal(await spool.ack(first.event_id), true);
    assert.deepEqual(await spool.list(), [second.event_id]);
    assert.equal(await spool.ack(first.event_id), false);
    assert.deepEqual(await spool.read(second.event_id), second);
    await assert.rejects(spool.ack('../escape'), /event_id/i);
  });
});

test('display name expires at received_at plus 24 hours and rewrite remains 0600', async () => {
  await withSpool(async ({ rootDir, setNow, spool }) => {
    const event = safeEvent();
    await spool.put(event);
    const target = path.join(rootDir, `${event.event_id}.json`);
    const wrapper = JSON.parse(await readFile(target, 'utf8'));

    assert.equal(wrapper.display_name_expires_at, event.received_at + 24 * 60 * 60);
    setNow(wrapper.display_name_expires_at - 1);
    assert.equal((await spool.read(event.event_id)).display_name, 'RawPushName');
    assert.equal(await spool.expireDisplayNames(wrapper.display_name_expires_at - 1), 0);

    setNow(wrapper.display_name_expires_at);
    assert.deepEqual(await spool.put(event), {
      event_id: event.event_id,
      duplicate: true,
    });
    assert.equal(await spool.expireDisplayNames(wrapper.display_name_expires_at), 0);
    assert.equal('display_name' in (await spool.read(event.event_id)), false);
    assert.equal((await lstat(target)).mode & 0o777, 0o600);
    assert.equal(await spool.expireDisplayNames(wrapper.display_name_expires_at + 1), 0);
  });
});

test('read cannot return an expired display name even without an explicit expiry sweep', async () => {
  await withSpool(async ({ setNow, spool }) => {
    const event = safeEvent();
    await spool.put(event);
    setNow(event.received_at + 24 * 60 * 60);

    const returned = await spool.read(event.event_id);
    assert.equal('display_name' in returned, false);
  });
});

test('purge uses immutable spooled_at cutoff and replay does not extend retention', async () => {
  await withSpool(async ({ setNow, spool }) => {
    const retention = 72 * 60 * 60;
    setNow(CAPTURED_AT + 100);
    const oldEvent = safeEvent('retention-old');
    await spool.put(oldEvent);
    setNow(CAPTURED_AT + retention + 200);
    const newEvent = safeEvent('retention-new');
    await spool.put(newEvent);
    setNow(CAPTURED_AT + retention + 300);
    await spool.put(oldEvent);

    const cutoff = CAPTURED_AT + retention + 300 - retention;
    assert.deepEqual(await spool.purgeOlderThan(cutoff), {
      events: 1,
      quarantine: 0,
    });
    assert.deepEqual(await spool.list(), [newEvent.event_id]);
  });
});

test('purge ignores symlinks and non-regular files', async () => {
  await withSpool(async ({ rootDir, spool, testRoot }) => {
    await spool.list();
    const outside = path.join(testRoot, 'outside-retention.json');
    const linkedId = IDS.eventId('observer-spool-test', 'purge-link');
    await writeFile(outside, 'outside-stays');
    await symlink(outside, path.join(rootDir, `${linkedId}.json`));
    await mkdir(path.join(rootDir, `${IDS.eventId('observer-spool-test', 'directory')}.json`));

    assert.deepEqual(await spool.purgeOlderThan(Number.MAX_SAFE_INTEGER), {
      events: 0,
      quarantine: 0,
    });
    assert.equal(await readFile(outside, 'utf8'), 'outside-stays');
  });
});

test('root symlink and non-directory roots are rejected', async () => {
  const testRoot = await mkdtemp(path.join(HERE, '.spool-root-test-'));
  try {
    const actual = path.join(testRoot, 'actual');
    const linked = path.join(testRoot, 'linked');
    const fileRoot = path.join(testRoot, 'file-root');
    await mkdir(actual, { mode: 0o700 });
    await symlink(actual, linked);
    await writeFile(fileRoot, 'not-directory');

    await assert.rejects(new SafeSpool(linked).list(), /root|symlink/i);
    await assert.rejects(new SafeSpool(fileRoot).list(), /root|directory/i);
  } finally {
    await rm(testRoot, { recursive: true, force: true });
  }
});

test('tests use only local temporary roots, never real observer paths', async () => {
  await withSpool(async ({ rootDir, spool }) => {
    await spool.list();
    assert.equal(rootDir.startsWith(HERE), true);
    assert.notEqual(rootDir, '/var/lib/brain/whatsapp-observer/outbox');
    assert.notEqual(rootDir, '/var/lib/brain/whatsapp-observer/session');
    assert.notEqual(rootDir, '/root/.hermes/platforms/whatsapp/session');
  });
});
