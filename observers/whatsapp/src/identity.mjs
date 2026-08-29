import { randomBytes } from 'node:crypto';
import {
  chmod,
  lstat,
  mkdir,
  open,
  readFile,
  readdir,
  rename,
  unlink,
} from 'node:fs/promises';
import path from 'node:path';

const PHONE_JID = /^([1-9][0-9]{6,14})@s\.whatsapp\.net$/;
const LID_JID = /^([0-9]{1,20})@lid$/;
const PHONE = /^[1-9][0-9]{6,14}$/;
const LID = /^[0-9]{1,20}$/;
const FORWARD_FILE = /^lid-mapping-([1-9][0-9]{6,14})\.json$/;
const REVERSE_FILE = /^lid-mapping-([0-9]{1,20})_reverse\.json$/;
const MAPPING_PREFIX = 'lid-mapping-';
const MAX_MAPPING_BYTES = 4096;

function phoneFromJid(value) {
  return typeof value === 'string' ? PHONE_JID.exec(value)?.[1] ?? null : null;
}

function lidFromJid(value) {
  return typeof value === 'string' ? LID_JID.exec(value)?.[1] ?? null : null;
}

export function deriveIdentityEvidence(msg) {
  if (msg === null || typeof msg !== 'object') {
    return null;
  }
  const remoteJid = msg.key?.remoteJid;
  const directPhone = phoneFromJid(remoteJid);
  if (directPhone !== null) {
    return { remoteJid, phoneJid: remoteJid, lid: null };
  }

  const lid = lidFromJid(remoteJid);
  if (lid === null) {
    return null;
  }

  const alternatives = new Set();
  for (const candidate of [msg.key?.remoteJidAlt, msg.key?.participantAlt]) {
    const phone = phoneFromJid(candidate);
    if (phone !== null) {
      alternatives.add(`${phone}@s.whatsapp.net`);
    }
  }
  return {
    remoteJid,
    phoneJid: alternatives.size === 1 ? alternatives.values().next().value : null,
    lid: `${lid}@lid`,
  };
}

export function contactKeyForEvidence(evidence, transportIds) {
  if (evidence === null || typeof evidence !== 'object') {
    return null;
  }
  const phone = phoneFromJid(evidence.phoneJid);
  if (phone === null || typeof transportIds?.contactKey !== 'function') {
    return null;
  }
  try {
    return transportIds.contactKey(phone);
  } catch {
    return null;
  }
}

async function ensurePrivateDirectory(sessionDir) {
  let created = false;
  try {
    const existing = await lstat(sessionDir);
    if (existing.isSymbolicLink() || !existing.isDirectory()) {
      throw new Error('mapping directory is not a real directory');
    }
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      throw error;
    }
    await mkdir(sessionDir, { recursive: true, mode: 0o700 });
    created = true;
    const createdEntry = await lstat(sessionDir);
    if (createdEntry.isSymbolicLink() || !createdEntry.isDirectory()) {
      throw new Error('mapping directory creation was unsafe');
    }
  }
  if (created) {
    await chmod(sessionDir, 0o700);
  }
}

function parseMapping(name, raw) {
  if (raw.byteLength > MAX_MAPPING_BYTES) {
    throw new Error('mapping is too large');
  }
  let value;
  try {
    value = JSON.parse(raw.toString('utf8'));
  } catch {
    throw new Error('mapping JSON is invalid');
  }
  if (typeof value !== 'string') {
    throw new Error('mapping must contain a JSON string');
  }

  const forward = FORWARD_FILE.exec(name);
  if (forward !== null && LID.test(value)) {
    return { phone: forward[1], lid: value };
  }
  const reverse = REVERSE_FILE.exec(name);
  if (reverse !== null && PHONE.test(value)) {
    return { phone: value, lid: reverse[1] };
  }
  throw new Error('mapping filename or value is invalid');
}

async function inspectMappings(sessionDir) {
  const entries = await readdir(sessionDir, { withFileTypes: true });
  const mappings = [];
  for (const entry of entries) {
    if (!entry.name.startsWith(MAPPING_PREFIX)) {
      continue;
    }
    if (!FORWARD_FILE.test(entry.name) && !REVERSE_FILE.test(entry.name)) {
      throw new Error('mapping filename is not allowlisted');
    }
    const mappingPath = path.join(sessionDir, entry.name);
    const metadata = await lstat(mappingPath);
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error('mapping is not a regular file');
    }
    const raw = await readFile(mappingPath);
    mappings.push({ name: entry.name, ...parseMapping(entry.name, raw) });
  }
  return mappings;
}

async function syncDirectory(sessionDir) {
  let directory;
  try {
    directory = await open(sessionDir, 'r');
    await directory.sync();
  } catch (error) {
    if (!['EINVAL', 'ENOTSUP', 'EISDIR'].includes(error?.code)) {
      throw error;
    }
  } finally {
    await directory?.close();
  }
}

async function atomicCreate(sessionDir, targetName, value) {
  const targetPath = path.join(sessionDir, targetName);
  const tempName = `.observer-mapping-${process.pid}-${randomBytes(12).toString('hex')}.tmp`;
  const tempPath = path.join(sessionDir, tempName);
  let temp;
  try {
    temp = await open(tempPath, 'wx', 0o600);
    await temp.writeFile(JSON.stringify(value), { encoding: 'utf8' });
    await temp.chmod(0o600);
    await temp.sync();
    await temp.close();
    temp = null;

    try {
      await lstat(targetPath);
      const conflict = new Error('mapping target appeared concurrently');
      conflict.code = 'MAPPING_CONFLICT';
      throw conflict;
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
    }
    await rename(tempPath, targetPath);
    await syncDirectory(sessionDir);
  } finally {
    await temp?.close().catch(() => {});
    await unlink(tempPath).catch((error) => {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
    });
  }
}

export async function persistLidMapping(sessionDir, evidence) {
  const phone = phoneFromJid(evidence?.phoneJid);
  const lid = lidFromJid(evidence?.lid);
  if (
    phone === null ||
    lid === null ||
    evidence?.remoteJid !== evidence.lid ||
    typeof sessionDir !== 'string' ||
    sessionDir.length === 0
  ) {
    return { status: 'unresolved' };
  }

  try {
    await ensurePrivateDirectory(sessionDir);
    const mappings = await inspectMappings(sessionDir);
    if (
      mappings.some(
        (mapping) =>
          (mapping.phone === phone && mapping.lid !== lid) ||
          (mapping.lid === lid && mapping.phone !== phone),
      )
    ) {
      return { status: 'conflict' };
    }

    const forwardName = `lid-mapping-${phone}.json`;
    const reverseName = `lid-mapping-${lid}_reverse.json`;
    const forwardExists = mappings.some((mapping) => mapping.name === forwardName);
    const reverseExists = mappings.some((mapping) => mapping.name === reverseName);
    if (forwardExists && reverseExists) {
      return { status: 'unchanged' };
    }
    if (!forwardExists) {
      await atomicCreate(sessionDir, forwardName, lid);
    }
    if (!reverseExists) {
      await atomicCreate(sessionDir, reverseName, phone);
    }
    return { status: 'written' };
  } catch (error) {
    if (error?.code === 'MAPPING_CONFLICT') {
      return { status: 'conflict' };
    }
    try {
      const metadata = await lstat(sessionDir);
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        return { status: 'conflict' };
      }
      await inspectMappings(sessionDir);
    } catch {
      return { status: 'conflict' };
    }
    return { status: 'retryable' };
  }
}
