import assert from 'node:assert/strict';
import { chmod, lstat, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { TransportIds } from '../src/hmac.mjs';
import {
  contactKeyForEvidence,
  deriveIdentityEvidence,
  persistLidMapping,
} from '../src/identity.mjs';
import { normalizeInboundMessage } from '../src/normalize.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '../../..');
const IDS = new TransportIds(Buffer.from('t'.repeat(32), 'utf8'));
const PHONE = '15551234567';
const OTHER_PHONE = '15557654321';
const PHONE_JID = `${PHONE}@s.whatsapp.net`;
const OTHER_PHONE_JID = `${OTHER_PHONE}@s.whatsapp.net`;
const LID = '123456789012345';
const LID_JID = `${LID}@lid`;

function message(key = {}, overrides = {}) {
  return {
    key: {
      id: 'synthetic-message-id',
      remoteJid: PHONE_JID,
      fromMe: false,
      ...key,
    },
    messageTimestamp: 1_800_000_000,
    pushName: 'Synthetic Name',
    message: { conversation: 'synthetic body' },
    ...overrides,
  };
}

async function withSessionDir(callback) {
  const root = await mkdtemp(path.join(HERE, '.identity-test-'));
  const sessionDir = path.join(root, 'session');
  try {
    return await callback(sessionDir, root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test('direct numeric PN yields canonical in-memory evidence', () => {
  assert.deepEqual(deriveIdentityEvidence(message()), {
    remoteJid: PHONE_JID,
    phoneJid: PHONE_JID,
    lid: null,
  });
});

test('direct numeric LID accepts one explicit PN alternative', () => {
  assert.deepEqual(
    deriveIdentityEvidence(message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID })),
    { remoteJid: LID_JID, phoneJid: PHONE_JID, lid: LID_JID },
  );
});

test('LID without explicit PN evidence remains unresolved', () => {
  assert.deepEqual(deriveIdentityEvidence(message({ remoteJid: LID_JID })), {
    remoteJid: LID_JID,
    phoneJid: null,
    lid: LID_JID,
  });
});

test('malformed, group, status, broadcast, and nonnumeric identities are rejected', () => {
  for (const remoteJid of [
    '',
    '123456789@g.us',
    'status@broadcast',
    '123456789@broadcast',
    'newsletter@newsletter',
    'phone@s.whatsapp.net',
    'notnumeric@lid',
    `123\u0000@s.whatsapp.net`,
    '+15551234567@s.whatsapp.net',
  ]) {
    assert.equal(deriveIdentityEvidence(message({ remoteJid })), null, remoteJid);
  }
});

test('equivalent explicit PN alternatives collapse to one identity', () => {
  assert.deepEqual(
    deriveIdentityEvidence(
      message({
        remoteJid: LID_JID,
        remoteJidAlt: PHONE_JID,
        participantAlt: PHONE_JID,
      }),
    ),
    { remoteJid: LID_JID, phoneJid: PHONE_JID, lid: LID_JID },
  );
});

test('conflicting explicit PN alternatives fail closed', () => {
  assert.deepEqual(
    deriveIdentityEvidence(
      message({
        remoteJid: LID_JID,
        remoteJidAlt: PHONE_JID,
        participantAlt: OTHER_PHONE_JID,
      }),
    ),
    { remoteJid: LID_JID, phoneJid: null, lid: LID_JID },
  );
});

test('pushName and body digits never become identity evidence', () => {
  const evidence = deriveIdentityEvidence(
    message(
      { remoteJid: LID_JID },
      {
        pushName: `Identity ${OTHER_PHONE}`,
        message: { conversation: `body contains ${OTHER_PHONE}` },
      },
    ),
  );

  assert.equal(evidence.phoneJid, null);
  assert.equal(JSON.stringify(evidence).includes(OTHER_PHONE), false);
});

test('contact proof uses only canonical PN evidence', () => {
  const direct = deriveIdentityEvidence(message());
  const mapped = deriveIdentityEvidence(
    message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
  );
  const unresolved = deriveIdentityEvidence(message({ remoteJid: LID_JID }));
  const conflicting = deriveIdentityEvidence(
    message({
      remoteJid: LID_JID,
      remoteJidAlt: PHONE_JID,
      participantAlt: OTHER_PHONE_JID,
    }),
  );

  assert.equal(contactKeyForEvidence(direct, IDS), IDS.contactKey(PHONE));
  assert.equal(contactKeyForEvidence(mapped, IDS), IDS.contactKey(PHONE));
  assert.equal(contactKeyForEvidence(unresolved, IDS), null);
  assert.equal(contactKeyForEvidence(conflicting, IDS), null);
  assert.equal(contactKeyForEvidence(null, IDS), null);
});

test('complete PN/LID evidence writes the exact Brain mapping contract atomically', async () => {
  await withSessionDir(async (sessionDir) => {
    const evidence = deriveIdentityEvidence(
      message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
    );
    const result = await persistLidMapping(sessionDir, evidence);
    const forward = path.join(sessionDir, `lid-mapping-${PHONE}.json`);
    const reverse = path.join(sessionDir, `lid-mapping-${LID}_reverse.json`);

    assert.deepEqual(result, { status: 'written' });
    assert.deepEqual((await readdir(sessionDir)).sort(), [
      `lid-mapping-${LID}_reverse.json`,
      `lid-mapping-${PHONE}.json`,
    ]);
    assert.equal(await readFile(forward, 'utf8'), JSON.stringify(LID));
    assert.equal(await readFile(reverse, 'utf8'), JSON.stringify(PHONE));
    assert.equal((await lstat(sessionDir)).mode & 0o777, 0o700);
    assert.equal((await lstat(forward)).mode & 0o777, 0o600);
    assert.equal((await lstat(reverse)).mode & 0o777, 0o600);
  });
});

test('direct PN, unresolved LID, and conflicting evidence write no mappings', async () => {
  await withSessionDir(async (sessionDir) => {
    for (const evidence of [
      deriveIdentityEvidence(message()),
      deriveIdentityEvidence(message({ remoteJid: LID_JID })),
      deriveIdentityEvidence(
        message({
          remoteJid: LID_JID,
          remoteJidAlt: PHONE_JID,
          participantAlt: OTHER_PHONE_JID,
        }),
      ),
    ]) {
      assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
        status: 'unresolved',
      });
    }
    await assert.rejects(lstat(sessionDir), { code: 'ENOENT' });
  });
});

test('identical replay is an idempotent no-op', async () => {
  await withSessionDir(async (sessionDir) => {
    const evidence = deriveIdentityEvidence(
      message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
    );
    assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
      status: 'written',
    });
    assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
      status: 'unchanged',
    });
  });
});

test('conflicting or malformed existing mappings are never overwritten', async () => {
  await withSessionDir(async (sessionDir) => {
    await mkdir(sessionDir, { mode: 0o700 });
    const target = path.join(sessionDir, `lid-mapping-${PHONE}.json`);
    const evidence = deriveIdentityEvidence(
      message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
    );

    await writeFile(target, JSON.stringify('999999999999999'), { mode: 0o600 });
    assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
      status: 'conflict',
    });
    assert.equal(await readFile(target, 'utf8'), JSON.stringify('999999999999999'));

    await writeFile(target, '{malformed', { mode: 0o600 });
    assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
      status: 'conflict',
    });
    assert.equal(await readFile(target, 'utf8'), '{malformed');
  });
});

test('mapping target symlinks are rejected without following or replacing them', async () => {
  await withSessionDir(async (sessionDir, root) => {
    await mkdir(sessionDir, { mode: 0o700 });
    const outside = path.join(root, 'outside.json');
    const target = path.join(sessionDir, `lid-mapping-${PHONE}.json`);
    await writeFile(outside, JSON.stringify('outside-remains'), { mode: 0o600 });
    await symlink(outside, target);
    const evidence = deriveIdentityEvidence(
      message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
    );

    assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
      status: 'conflict',
    });
    assert.equal((await lstat(target)).isSymbolicLink(), true);
    assert.equal(await readFile(outside, 'utf8'), JSON.stringify('outside-remains'));
  });
});

test('Node-created mappings are readable by the existing Brain Python resolver', async () => {
  await withSessionDir(async (sessionDir) => {
    const evidence = deriveIdentityEvidence(
      message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
    );
    assert.deepEqual(await persistLidMapping(sessionDir, evidence), {
      status: 'written',
    });
    const probe = spawnSync(
      path.join(REPO_ROOT, '.venv/bin/python'),
      [
        '-c',
        'import json,sys; from pathlib import Path; from brain.transport_models import RuntimeIds; from brain.whatsapp_identity import resolve_phone,verify_transport_identity; ids=RuntimeIds(b"t"*32); r=resolve_phone(sys.argv[1],Path(sys.argv[2])); v=verify_transport_identity(remote_jid_hmac=ids.jid_hmac(sys.argv[1]),contact_key=None,mapping_dir=Path(sys.argv[2]),transport_ids=ids); print(json.dumps({"resolve":{"status":r.status,"phone":r.phone,"reason":r.reason},"verify":{"status":v.status,"phone":v.phone,"contact_key":v.contact_key,"reason":v.reason}},sort_keys=True))',
        LID_JID,
        sessionDir,
      ],
      {
        cwd: REPO_ROOT,
        env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') },
        encoding: 'utf8',
      },
    );

    assert.equal(probe.status, 0, probe.stderr);
    assert.deepEqual(JSON.parse(probe.stdout), {
      resolve: { phone: PHONE, reason: 'resolved', status: 'ok' },
      verify: {
        contact_key: IDS.contactKey(PHONE),
        phone: PHONE,
        reason: 'resolved',
        status: 'ok',
      },
    });
  });
});

test('normalizer uses identity evidence without leaking it into the safe event', () => {
  const direct = normalizeInboundMessage(message(), 1_800_000_001, IDS, 'observer-a');
  const mapped = normalizeInboundMessage(
    message({ remoteJid: LID_JID, remoteJidAlt: PHONE_JID }),
    1_800_000_001,
    IDS,
    'observer-a',
  );
  const unresolved = normalizeInboundMessage(
    message({ remoteJid: LID_JID }),
    1_800_000_001,
    IDS,
    'observer-a',
  );
  const conflicting = normalizeInboundMessage(
    message({
      remoteJid: LID_JID,
      remoteJidAlt: PHONE_JID,
      participantAlt: OTHER_PHONE_JID,
    }),
    1_800_000_001,
    IDS,
    'observer-a',
  );

  assert.equal(direct.contact_key, IDS.contactKey(PHONE));
  assert.equal(mapped.remote_jid_hmac, IDS.jidHmac(LID_JID));
  assert.equal(mapped.contact_key, IDS.contactKey(PHONE));
  assert.equal(unresolved.remote_jid_hmac, IDS.jidHmac(LID_JID));
  assert.equal('contact_key' in unresolved, false);
  assert.equal('contact_key' in conflicting, false);
  for (const safe of [direct, mapped, unresolved, conflicting]) {
    const serialized = JSON.stringify(safe);
    for (const raw of [PHONE_JID, LID_JID, PHONE, OTHER_PHONE_JID, OTHER_PHONE]) {
      assert.equal(serialized.includes(raw), false, raw);
    }
    for (const key of ['remoteJid', 'phoneJid', 'lid', 'remoteJidAlt', 'participantAlt']) {
      assert.equal(key in safe, false, key);
    }
  }
});

test('normalizer preserves Task 1 body and CTWA behavior with identity integration', () => {
  const body = 'CTWA body 😀';
  const raw = message(
    { remoteJid: LID_JID, remoteJidAlt: PHONE_JID },
    {
      message: {
        extendedTextMessage: {
          text: body,
          contextInfo: {
            externalAdReply: {
              sourceType: 'ad',
              sourceId: 'synthetic-source',
              clickToWhatsappCall: true,
            },
          },
        },
      },
    },
  );
  const safe = normalizeInboundMessage(raw, 1_800_000_001, IDS, 'observer-a');

  assert.equal(safe.body_hmac, IDS.bodyHmac(body));
  assert.equal(safe.body_length, Array.from(body).length);
  assert.equal(safe.transport_kind, 'ctwa_candidate');
  assert.equal(safe.external_ad_reply.source_id_hmac, IDS.opaqueHmac('synthetic-source'));
  assert.deepEqual(safe.external_ad_reply_raw, {
    clickToWhatsappCall: true,
    sourceId: 'synthetic-source',
    sourceType: 'ad',
  });
  assert.equal(JSON.stringify(safe).includes(body), false);
});

test('unknown raw attribution fields do not affect CTWA classification or identity evidence', () => {
  const base = message({}, {
    message: {
      extendedTextMessage: {
        text: 'synthetic body',
        contextInfo: {
          externalAdReply: { sourceType: 'ad', sourceId: 'known-signal' },
        },
      },
    },
  });
  const withUnknown = structuredClone(base);
  withUnknown.message.extendedTextMessage.contextInfo.externalAdReply.unrecognizedNested = {
    identity: OTHER_PHONE,
    arbitrary: ['field'],
  };

  const expected = normalizeInboundMessage(base, 1_800_000_001, IDS, 'observer-a');
  const actual = normalizeInboundMessage(withUnknown, 1_800_000_001, IDS, 'observer-a');

  assert.equal(actual.transport_kind, expected.transport_kind);
  assert.equal(actual.remote_jid_hmac, expected.remote_jid_hmac);
  assert.equal(actual.contact_key, expected.contact_key);
  assert.deepEqual(actual.external_ad_reply, expected.external_ad_reply);
  assert.deepEqual(actual.external_ad_reply_raw.unrecognizedNested, {
    arbitrary: ['field'],
    identity: OTHER_PHONE,
  });
});

test('test directories never target real Hermes or observer session paths', async () => {
  await withSessionDir(async (sessionDir) => {
    assert.equal(sessionDir.startsWith(HERE), true);
    assert.notEqual(sessionDir, '/root/.hermes/platforms/whatsapp/session');
    assert.notEqual(sessionDir, '/var/lib/brain/whatsapp-observer/session');
    await mkdir(sessionDir, { mode: 0o777 });
    await chmod(sessionDir, 0o700);
  });
});
