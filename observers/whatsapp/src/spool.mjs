import { randomBytes } from 'node:crypto';
import { constants } from 'node:fs';
import {
  chmod,
  lstat,
  mkdir,
  open,
  readdir,
  rename,
  unlink,
} from 'node:fs/promises';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';

const EVENT_ID = /^waevt_[0-9a-f]{64}$/;
const EVENT_FILE = /^(waevt_[0-9a-f]{64})\.json$/;
const HMAC = /^[0-9a-f]{64}$/;
const OBSERVER_DEVICE_ID = /^[!-~]{1,128}$/;
const HOSTNAME = /^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/;
const TOP_LEVEL_FIELDS = new Set([
  'event_id',
  'observer_device_id',
  'received_at',
  'message_timestamp',
  'remote_jid_hmac',
  'contact_key',
  'body_hmac',
  'body_length',
  'display_name',
  'native_type',
  'transport_kind',
  'external_ad_reply',
]);
const REQUIRED_FIELDS = [
  'event_id',
  'observer_device_id',
  'received_at',
  'remote_jid_hmac',
  'body_hmac',
  'body_length',
  'native_type',
  'transport_kind',
];
const EXTERNAL_FIELDS = new Set([
  'source_type',
  'source_app',
  'source_id_present',
  'source_id_length',
  'source_id_hmac',
  'source_url_hostname',
  'source_url_length',
  'source_url_hmac',
  'ctwa_clid_present',
  'ctwa_clid_length',
  'ctwa_clid_hmac',
  'show_ad_attribution',
  'click_to_whatsapp_call',
  'contains_auto_reply',
]);
const WRAPPER_FIELDS = new Set([
  'spool_version',
  'spooled_at',
  'display_name_expires_at',
  'event',
]);
const DISPLAY_NAME_TTL_SECONDS = 24 * 60 * 60;
const MAX_SAFE_LENGTH = 10_000_000;
const MAX_EVENT_BYTES = 16_384;
const MAX_RECORD_BYTES = 32_768;

function invalidSafeEvent() {
  throw new TypeError('safe event is invalid');
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function exactKeys(value, allowlist) {
  return Object.keys(value).every((key) => allowlist.has(key));
}

function finitePositive(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

function boundedInteger(value) {
  return Number.isInteger(value) && value >= 0 && value <= MAX_SAFE_LENGTH;
}

function boundedText(value, maximum) {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    Array.from(value).length <= maximum &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function optionalMetadata(value) {
  return value === undefined || value === null || boundedText(value, 128);
}

function validatePresence(external, prefix) {
  const present = external[`${prefix}_present`];
  const rawLength = external[`${prefix}_length`];
  const rawDigest = external[`${prefix}_hmac`];
  const length = rawLength === null ? undefined : rawLength;
  const digest = rawDigest === null ? undefined : rawDigest;
  if (present !== undefined && typeof present !== 'boolean') {
    invalidSafeEvent();
  }
  if (length !== undefined && !boundedInteger(length)) {
    invalidSafeEvent();
  }
  if (digest !== undefined && (typeof digest !== 'string' || !HMAC.test(digest))) {
    invalidSafeEvent();
  }
  if (present === true && (!(length > 0) || digest === undefined)) {
    invalidSafeEvent();
  }
  if (present === false && (length !== undefined || digest !== undefined)) {
    invalidSafeEvent();
  }
  if (present === undefined && (length !== undefined || digest !== undefined)) {
    invalidSafeEvent();
  }
}

function validateExternal(value) {
  if (!isObject(value) || !exactKeys(value, EXTERNAL_FIELDS)) {
    invalidSafeEvent();
  }
  if (!optionalMetadata(value.source_type) || !optionalMetadata(value.source_app)) {
    invalidSafeEvent();
  }
  validatePresence(value, 'source_id');
  validatePresence(value, 'ctwa_clid');

  const hostname = value.source_url_hostname ?? undefined;
  const urlLength = value.source_url_length ?? undefined;
  const urlHmac = value.source_url_hmac ?? undefined;
  const hasHostname = hostname !== undefined;
  const hasLength = urlLength !== undefined;
  const hasHmac = urlHmac !== undefined;
  if (hasHostname !== hasLength || hasHostname !== hasHmac) {
    invalidSafeEvent();
  }
  if (
    hasHostname &&
    (!boundedText(hostname, 253) ||
      hostname !== hostname.toLowerCase() ||
      !HOSTNAME.test(hostname) ||
      !boundedInteger(urlLength) ||
      urlLength <= 0 ||
      typeof urlHmac !== 'string' ||
      !HMAC.test(urlHmac))
  ) {
    invalidSafeEvent();
  }
  for (const field of [
    'show_ad_attribution',
    'click_to_whatsapp_call',
    'contains_auto_reply',
  ]) {
    if (value[field] !== undefined && typeof value[field] !== 'boolean') {
      invalidSafeEvent();
    }
  }
  return value;
}

function expectedTransportKind(external) {
  return external?.source_type === 'ad' &&
    (external.click_to_whatsapp_call === true ||
      external.ctwa_clid_present === true ||
      external.source_id_present === true)
    ? 'ctwa_candidate'
    : 'ordinary_inbound';
}

function validateSafeEvent(input) {
  let event;
  try {
    event = structuredClone(input);
  } catch {
    invalidSafeEvent();
  }
  if (
    !isObject(event) ||
    !exactKeys(event, TOP_LEVEL_FIELDS) ||
    !REQUIRED_FIELDS.every((field) => Object.hasOwn(event, field)) ||
    typeof event.event_id !== 'string' ||
    !EVENT_ID.test(event.event_id) ||
    typeof event.observer_device_id !== 'string' ||
    !OBSERVER_DEVICE_ID.test(event.observer_device_id) ||
    !finitePositive(event.received_at) ||
    typeof event.remote_jid_hmac !== 'string' ||
    !HMAC.test(event.remote_jid_hmac) ||
    typeof event.body_hmac !== 'string' ||
    !HMAC.test(event.body_hmac) ||
    !boundedInteger(event.body_length) ||
    !boundedText(event.native_type, 128) ||
    !['ctwa_candidate', 'ordinary_inbound'].includes(event.transport_kind)
  ) {
    invalidSafeEvent();
  }
  if (
    Object.hasOwn(event, 'message_timestamp') &&
    event.message_timestamp !== null &&
    !finitePositive(event.message_timestamp)
  ) {
    invalidSafeEvent();
  }
  if (
    Object.hasOwn(event, 'contact_key') &&
    event.contact_key !== null &&
    (typeof event.contact_key !== 'string' || !HMAC.test(event.contact_key))
  ) {
    invalidSafeEvent();
  }
  if (
    Object.hasOwn(event, 'display_name') &&
    event.display_name !== null &&
    !boundedText(event.display_name, 160)
  ) {
    invalidSafeEvent();
  }
  let external;
  if (Object.hasOwn(event, 'external_ad_reply')) {
    if (event.external_ad_reply === null) {
      invalidSafeEvent();
    }
    external = validateExternal(event.external_ad_reply);
  }
  if (event.transport_kind !== expectedTransportKind(external)) {
    invalidSafeEvent();
  }
  if (Buffer.byteLength(JSON.stringify(event), 'utf8') > MAX_EVENT_BYTES) {
    invalidSafeEvent();
  }
  return event;
}

function validateEventId(eventId) {
  if (typeof eventId !== 'string' || !EVENT_ID.test(eventId)) {
    throw new TypeError('event_id is invalid');
  }
  return eventId;
}

function withoutDisplayName(event) {
  const copy = { ...event };
  delete copy.display_name;
  return copy;
}

function validateWrapper(input) {
  if (
    !isObject(input) ||
    !exactKeys(input, WRAPPER_FIELDS) ||
    Object.keys(input).length !== WRAPPER_FIELDS.size ||
    input.spool_version !== 1 ||
    !finitePositive(input.spooled_at) ||
    (input.display_name_expires_at !== null &&
      !finitePositive(input.display_name_expires_at))
  ) {
    throw new Error('spool record is invalid');
  }
  const event = validateSafeEvent(input.event);
  const hasDisplay = typeof event.display_name === 'string';
  if (
    hasDisplay !== (input.display_name_expires_at !== null) ||
    (hasDisplay &&
      input.display_name_expires_at !==
        event.received_at + DISPLAY_NAME_TTL_SECONDS)
  ) {
    throw new Error('spool record is invalid');
  }
  return {
    spool_version: 1,
    spooled_at: input.spooled_at,
    display_name_expires_at: input.display_name_expires_at,
    event,
  };
}

async function syncDirectory(rootDir) {
  let directory;
  try {
    directory = await open(rootDir, 'r');
    await directory.sync();
  } catch (error) {
    if (!['EINVAL', 'ENOTSUP', 'EISDIR'].includes(error?.code)) {
      throw error;
    }
  } finally {
    await directory?.close();
  }
}

export class SafeSpool {
  #now;
  #rootDir;

  constructor(rootDir, { now = () => Date.now() / 1000 } = {}) {
    if (
      typeof rootDir !== 'string' ||
      !path.isAbsolute(rootDir) ||
      path.resolve(rootDir) !== rootDir ||
      rootDir === path.parse(rootDir).root ||
      typeof now !== 'function'
    ) {
      throw new TypeError('spool root is invalid');
    }
    this.#rootDir = rootDir;
    this.#now = now;
  }

  #clock() {
    const now = this.#now();
    if (!finitePositive(now)) {
      throw new Error('spool clock is invalid');
    }
    return now;
  }

  async #ensureRoot() {
    try {
      const metadata = await lstat(this.#rootDir);
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        throw new Error('spool root must be a real directory, not a symlink');
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
      await mkdir(this.#rootDir, { recursive: true, mode: 0o700 });
      const metadata = await lstat(this.#rootDir);
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        throw new Error('spool root creation is unsafe');
      }
    }
    await chmod(this.#rootDir, 0o700);
  }

  #target(eventId) {
    return path.join(this.#rootDir, `${validateEventId(eventId)}.json`);
  }

  async #atomicWrite(eventId, wrapper, replaceExisting) {
    const target = this.#target(eventId);
    const temp = path.join(
      this.#rootDir,
      `.${eventId}.${randomBytes(12).toString('hex')}.tmp`,
    );
    let handle;
    try {
      handle = await open(temp, 'wx', 0o600);
      await handle.writeFile(JSON.stringify(wrapper), { encoding: 'utf8' });
      await handle.chmod(0o600);
      await handle.sync();
      await handle.close();
      handle = null;

      if (!replaceExisting) {
        try {
          await lstat(target);
          throw new Error('event_id conflicts with an existing spool record');
        } catch (error) {
          if (error?.code !== 'ENOENT') {
            throw error;
          }
        }
      } else {
        const metadata = await lstat(target);
        if (metadata.isSymbolicLink() || !metadata.isFile()) {
          throw new Error('spool record is not a regular file');
        }
      }
      await rename(temp, target);
      await syncDirectory(this.#rootDir);
    } finally {
      await handle?.close().catch(() => {});
      await unlink(temp).catch((error) => {
        if (error?.code !== 'ENOENT') {
          throw error;
        }
      });
    }
  }

  async #readRecord(eventId) {
    const target = this.#target(eventId);
    let handle;
    try {
      const metadata = await lstat(target);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('spool record is not a regular file or is a symlink');
      }
      if (metadata.size > MAX_RECORD_BYTES) {
        throw new Error('spool record is too large');
      }
      handle = await open(target, constants.O_RDONLY | constants.O_NOFOLLOW);
      const opened = await handle.stat();
      if (!opened.isFile() || opened.size > MAX_RECORD_BYTES) {
        throw new Error('spool record is not a regular file');
      }
      const raw = await handle.readFile();
      let parsed;
      try {
        parsed = JSON.parse(raw.toString('utf8'));
      } catch {
        throw new Error('spool record JSON is invalid');
      }
      const wrapper = validateWrapper(parsed);
      if (wrapper.event.event_id !== eventId) {
        throw new Error('spool record event_id is invalid');
      }
      return wrapper;
    } catch (error) {
      if (error?.code === 'ENOENT') {
        throw new Error('spool record is unavailable');
      }
      throw error;
    } finally {
      await handle?.close();
    }
  }

  async put(safeEvent) {
    const event = validateSafeEvent(safeEvent);
    await this.#ensureRoot();
    const now = this.#clock();
    const target = this.#target(event.event_id);
    try {
      const metadata = await lstat(target);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('event_id conflicts with a non-regular spool record');
      }
      const existing = await this.#readRecord(event.event_id);
      const sameAfterExpiry =
        existing.display_name_expires_at === null &&
        isDeepStrictEqual(existing.event, withoutDisplayName(event));
      if (!isDeepStrictEqual(existing.event, event) && !sameAfterExpiry) {
        throw new Error('event_id conflicts with an existing spool record');
      }
      if (
        existing.display_name_expires_at !== null &&
        existing.display_name_expires_at <= now
      ) {
        await this.#atomicWrite(
          event.event_id,
          {
            ...existing,
            display_name_expires_at: null,
            event: withoutDisplayName(existing.event),
          },
          true,
        );
      }
      return { event_id: event.event_id, duplicate: true };
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
    }

    let storedEvent = event;
    let displayExpiry = null;
    if (typeof event.display_name === 'string') {
      displayExpiry = event.received_at + DISPLAY_NAME_TTL_SECONDS;
      if (displayExpiry <= now) {
        storedEvent = withoutDisplayName(event);
        displayExpiry = null;
      }
    }
    const wrapper = {
      spool_version: 1,
      spooled_at: now,
      display_name_expires_at: displayExpiry,
      event: storedEvent,
    };
    await this.#atomicWrite(event.event_id, wrapper, false);
    return { event_id: event.event_id, duplicate: false };
  }

  async list() {
    await this.#ensureRoot();
    await this.expireDisplayNames(this.#clock());
    const entries = await readdir(this.#rootDir, { withFileTypes: true });
    return entries
      .filter((entry) => entry.isFile() && EVENT_FILE.test(entry.name))
      .map((entry) => EVENT_FILE.exec(entry.name)[1])
      .sort();
  }

  async read(eventId) {
    validateEventId(eventId);
    await this.#ensureRoot();
    let wrapper = await this.#readRecord(eventId);
    if (
      wrapper.display_name_expires_at !== null &&
      wrapper.display_name_expires_at <= this.#clock()
    ) {
      wrapper = {
        ...wrapper,
        display_name_expires_at: null,
        event: withoutDisplayName(wrapper.event),
      };
      await this.#atomicWrite(eventId, wrapper, true);
    }
    return structuredClone(wrapper.event);
  }

  async ack(eventId) {
    validateEventId(eventId);
    await this.#ensureRoot();
    const target = this.#target(eventId);
    try {
      const metadata = await lstat(target);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('spool record is not a regular file');
      }
      await unlink(target);
      await syncDirectory(this.#rootDir);
      return true;
    } catch (error) {
      if (error?.code === 'ENOENT') {
        return false;
      }
      throw error;
    }
  }

  async expireDisplayNames(now = this.#clock()) {
    if (!finitePositive(now)) {
      throw new TypeError('expiry cutoff is invalid');
    }
    await this.#ensureRoot();
    const entries = await readdir(this.#rootDir, { withFileTypes: true });
    let expired = 0;
    for (const entry of entries) {
      const match = entry.isFile() ? EVENT_FILE.exec(entry.name) : null;
      if (match === null) {
        continue;
      }
      const wrapper = await this.#readRecord(match[1]);
      if (
        wrapper.display_name_expires_at !== null &&
        wrapper.display_name_expires_at <= now
      ) {
        await this.#atomicWrite(
          match[1],
          {
            ...wrapper,
            display_name_expires_at: null,
            event: withoutDisplayName(wrapper.event),
          },
          true,
        );
        expired += 1;
      }
    }
    return expired;
  }

  async purgeOlderThan(cutoff) {
    if (!finitePositive(cutoff)) {
      throw new TypeError('retention cutoff is invalid');
    }
    await this.#ensureRoot();
    const entries = await readdir(this.#rootDir, { withFileTypes: true });
    let purged = 0;
    for (const entry of entries) {
      const match = entry.isFile() ? EVENT_FILE.exec(entry.name) : null;
      if (match === null) {
        continue;
      }
      const wrapper = await this.#readRecord(match[1]);
      if (wrapper.spooled_at < cutoff) {
        await unlink(this.#target(match[1]));
        purged += 1;
      }
    }
    if (purged > 0) {
      await syncDirectory(this.#rootDir);
    }
    return purged;
  }
}
