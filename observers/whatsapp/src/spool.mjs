import { randomBytes } from 'node:crypto';
import { constants } from 'node:fs';
import {
  chmod,
  link,
  lstat,
  mkdir,
  open,
  readdir,
  rename,
  unlink,
} from 'node:fs/promises';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import {
  DEFAULT_RAW_ATTRIBUTION_LIMITS,
  validateEncodedRawAttribution,
} from './raw-attribution.mjs';

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
  'external_ad_reply_raw',
  'observer_event_version',
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
const QUARANTINE_FIELDS = new Set([
  'quarantine_version',
  'event_id',
  'captured_at',
  'reason',
  'external_ad_reply_raw',
]);
const DISPLAY_NAME_TTL_SECONDS = 24 * 60 * 60;
const MAX_SAFE_LENGTH = 10_000_000;
const MAX_EVENT_BYTES = 5 * 1024 * 1024;
const MAX_RECORD_BYTES = MAX_EVENT_BYTES + 65_536;
const DEFAULT_QUARANTINE_MAX_BYTES = 32 * 1024 * 1024;
const QUARANTINE_DIRECTORY = 'quarantine';
const QUARANTINE_REASON = /^[a-z][a-z0-9_]{0,63}$/;

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

function validateSafeEvent(input, rawLimits, { requireV2 = false } = {}) {
  try {
    if (isObject(input) && Object.hasOwn(input, 'external_ad_reply_raw')) {
      validateEncodedRawAttribution(input.external_ad_reply_raw, rawLimits);
    }
  } catch {
    invalidSafeEvent();
  }
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
  const isV2 = event.observer_event_version === 2;
  if (
    (Object.hasOwn(event, 'observer_event_version') && !isV2) ||
    (requireV2 && !isV2)
  ) {
    invalidSafeEvent();
  }
  const hasExternal = Object.hasOwn(event, 'external_ad_reply');
  const hasRaw = Object.hasOwn(event, 'external_ad_reply_raw');
  if (!isV2 && hasRaw) {
    invalidSafeEvent();
  }
  if (isV2 && hasExternal !== hasRaw) {
    invalidSafeEvent();
  }
  let external;
  if (hasExternal) {
    if (event.external_ad_reply === null) {
      invalidSafeEvent();
    }
    external = validateExternal(event.external_ad_reply);
  }
  if (hasRaw) {
    try {
      validateEncodedRawAttribution(event.external_ad_reply_raw, rawLimits);
    } catch {
      invalidSafeEvent();
    }
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

function validateWrapper(input, rawLimits) {
  if (
    !isObject(input) ||
    !exactKeys(input, WRAPPER_FIELDS) ||
    Object.keys(input).length !== WRAPPER_FIELDS.size ||
    ![1, 2].includes(input.spool_version) ||
    !finitePositive(input.spooled_at) ||
    (input.display_name_expires_at !== null &&
      !finitePositive(input.display_name_expires_at))
  ) {
    throw new Error('spool record is invalid');
  }
  const event = validateSafeEvent(input.event, rawLimits, {
    requireV2: input.spool_version === 2,
  });
  if (input.spool_version === 1 && Object.hasOwn(event, 'observer_event_version')) {
    throw new Error('spool record is invalid');
  }
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
    spool_version: input.spool_version,
    spooled_at: input.spooled_at,
    display_name_expires_at: input.display_name_expires_at,
    event,
  };
}

function validateQuarantineRecord(input, rawLimits, maximumBytes) {
  try {
    if (
      isObject(input) &&
      input.external_ad_reply_raw !== null
    ) {
      validateEncodedRawAttribution(input.external_ad_reply_raw, rawLimits);
    }
  } catch {
    throw new TypeError('quarantine record is invalid');
  }
  let record;
  try {
    record = structuredClone(input);
  } catch {
    throw new TypeError('quarantine record is invalid');
  }
  if (
    !isObject(record) ||
    !exactKeys(record, QUARANTINE_FIELDS) ||
    Object.keys(record).length !== QUARANTINE_FIELDS.size ||
    record.quarantine_version !== 1 ||
    typeof record.event_id !== 'string' ||
    !EVENT_ID.test(record.event_id) ||
    !finitePositive(record.captured_at) ||
    typeof record.reason !== 'string' ||
    !QUARANTINE_REASON.test(record.reason) ||
    !Object.hasOwn(record, 'external_ad_reply_raw')
  ) {
    throw new TypeError('quarantine record is invalid');
  }
  if (record.external_ad_reply_raw !== null) {
    try {
      validateEncodedRawAttribution(record.external_ad_reply_raw, rawLimits);
    } catch {
      throw new TypeError('quarantine record is invalid');
    }
  }
  if (Buffer.byteLength(JSON.stringify(record), 'utf8') > maximumBytes) {
    throw new TypeError('quarantine record is invalid');
  }
  return record;
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
  #quarantineMaxBytes;
  #quarantineLimits;
  #rootDir;
  #rawLimits;

  constructor(
    rootDir,
    {
      now = () => Date.now() / 1000,
      rawLimits = DEFAULT_RAW_ATTRIBUTION_LIMITS,
      quarantineMaxBytes = DEFAULT_QUARANTINE_MAX_BYTES,
    } = {},
  ) {
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
    try {
      validateEncodedRawAttribution(null, rawLimits);
    } catch {
      throw new TypeError('raw attribution limits are invalid');
    }
    if (
      !Number.isSafeInteger(quarantineMaxBytes) ||
      quarantineMaxBytes < rawLimits.maxBytes ||
      quarantineMaxBytes > DEFAULT_QUARANTINE_MAX_BYTES
    ) {
      throw new TypeError('quarantine size limit is invalid');
    }
    this.#rawLimits = {
      maxBytes: rawLimits.maxBytes,
      maxDepth: rawLimits.maxDepth,
      maxNodes: rawLimits.maxNodes,
    };
    this.#quarantineMaxBytes = quarantineMaxBytes;
    this.#quarantineLimits = { ...this.#rawLimits, maxBytes: quarantineMaxBytes };
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

  #quarantineDir() {
    return path.join(this.#rootDir, QUARANTINE_DIRECTORY);
  }

  async #ensureQuarantineRoot() {
    await this.#ensureRoot();
    const quarantineDir = this.#quarantineDir();
    try {
      const metadata = await lstat(quarantineDir);
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        throw new Error('quarantine root must be a real directory, not a symlink');
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
      await mkdir(quarantineDir, { mode: 0o700 });
      const metadata = await lstat(quarantineDir);
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        throw new Error('quarantine root creation is unsafe');
      }
    }
    await chmod(quarantineDir, 0o700);
    return quarantineDir;
  }

  #target(eventId, directory = this.#rootDir) {
    return path.join(directory, `${validateEventId(eventId)}.json`);
  }

  async #atomicWrite(directory, eventId, wrapper, replaceExisting) {
    const target = this.#target(eventId, directory);
    const temp = path.join(
      directory,
      `.${eventId}.${randomBytes(12).toString('hex')}.tmp`,
    );
    let handle;
    let syncNeeded = false;
    try {
      handle = await open(temp, 'wx', 0o600);
      await handle.writeFile(JSON.stringify(wrapper), { encoding: 'utf8' });
      await handle.chmod(0o600);
      await handle.sync();
      await handle.close();
      handle = null;

      if (!replaceExisting) {
        try {
          await link(temp, target);
        } catch (error) {
          if (error?.code !== 'EEXIST') {
            throw error;
          }
          return false;
        }
      } else {
        const metadata = await lstat(target);
        if (metadata.isSymbolicLink() || !metadata.isFile()) {
          throw new Error('spool record is not a regular file');
        }
        await rename(temp, target);
      }
      syncNeeded = true;
      return true;
    } finally {
      await handle?.close().catch(() => {});
      try {
        await unlink(temp);
        syncNeeded = true;
      } catch (error) {
        if (error?.code !== 'ENOENT') {
          throw error;
        }
      }
      if (syncNeeded) {
        await syncDirectory(directory);
      }
    }
  }

  async #readRecord(eventId, {
    directory = this.#rootDir,
    maximumBytes = MAX_RECORD_BYTES,
    validate = (value) => validateWrapper(value, this.#rawLimits),
    unavailable = 'spool record is unavailable',
  } = {}) {
    const target = this.#target(eventId, directory);
    let handle;
    try {
      const metadata = await lstat(target);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('spool record is not a regular file or is a symlink');
      }
      if (metadata.size > maximumBytes) {
        throw new Error('spool record is too large');
      }
      handle = await open(target, constants.O_RDONLY | constants.O_NOFOLLOW);
      const opened = await handle.stat();
      if (!opened.isFile() || opened.size > maximumBytes) {
        throw new Error('spool record is not a regular file');
      }
      const raw = await handle.readFile();
      let parsed;
      try {
        parsed = JSON.parse(raw.toString('utf8'));
      } catch {
        throw new Error('spool record JSON is invalid');
      }
      const wrapper = validate(parsed);
      if (wrapper.event_id !== undefined ? wrapper.event_id !== eventId : wrapper.event.event_id !== eventId) {
        throw new Error('spool record event_id is invalid');
      }
      return wrapper;
    } catch (error) {
      if (error?.code === 'ENOENT') {
        throw new Error(unavailable);
      }
      throw error;
    } finally {
      await handle?.close();
    }
  }

  async #readQuarantineRecord(eventId, quarantineDir = this.#quarantineDir()) {
    return this.#readRecord(eventId, {
      directory: quarantineDir,
      maximumBytes: this.#quarantineMaxBytes,
      validate: (value) => validateQuarantineRecord(
        value,
        this.#quarantineLimits,
        this.#quarantineMaxBytes,
      ),
      unavailable: 'quarantine record is unavailable',
    });
  }

  async #publishQuarantineAtomically(record) {
    const quarantineDir = await this.#ensureQuarantineRoot();
    const target = this.#target(record.event_id, quarantineDir);
    try {
      const metadata = await lstat(target);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('event_id conflicts with a non-regular quarantine record');
      }
      const existing = await this.#readQuarantineRecord(
        record.event_id,
        quarantineDir,
      );
      if (!isDeepStrictEqual(existing, record)) {
        throw new Error('event_id conflicts with an existing quarantine record');
      }
      return { event_id: record.event_id, duplicate: true };
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
    }

    if (await this.#atomicWrite(
      quarantineDir,
      record.event_id,
      record,
      false,
    )) {
      return { event_id: record.event_id, duplicate: false };
    }

    const existing = await this.#readQuarantineRecord(
      record.event_id,
      quarantineDir,
    );
    if (!isDeepStrictEqual(existing, record)) {
      throw new Error('event_id conflicts with an existing quarantine record');
    }
    return { event_id: record.event_id, duplicate: true };
  }

  async put(safeEvent) {
    const event = validateSafeEvent(safeEvent, this.#rawLimits, { requireV2: true });
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
          this.#rootDir,
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
      spool_version: 2,
      spooled_at: now,
      display_name_expires_at: displayExpiry,
      event: storedEvent,
    };
    if (await this.#atomicWrite(this.#rootDir, event.event_id, wrapper, false)) {
      return { event_id: event.event_id, duplicate: false };
    }

    const existing = await this.#readRecord(event.event_id);
    const sameAfterExpiry =
      existing.display_name_expires_at === null &&
      isDeepStrictEqual(existing.event, withoutDisplayName(event));
    if (!isDeepStrictEqual(existing.event, event) && !sameAfterExpiry) {
      throw new Error('event_id conflicts with an existing spool record');
    }
    return { event_id: event.event_id, duplicate: true };
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
      await this.#atomicWrite(this.#rootDir, eventId, wrapper, true);
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

  async quarantine({
    event_id,
    captured_at,
    reason,
    external_ad_reply_raw = null,
  }) {
    const record = validateQuarantineRecord(
      {
        quarantine_version: 1,
        event_id,
        captured_at,
        reason,
        external_ad_reply_raw,
      },
      this.#quarantineLimits,
      this.#quarantineMaxBytes,
    );
    return this.#publishQuarantineAtomically(record);
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
          this.#rootDir,
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
    let events = 0;
    for (const entry of entries) {
      const match = entry.isFile() ? EVENT_FILE.exec(entry.name) : null;
      if (match === null) {
        continue;
      }
      const wrapper = await this.#readRecord(match[1]);
      if (wrapper.spooled_at < cutoff) {
        await unlink(this.#target(match[1]));
        events += 1;
      }
    }
    let quarantine = 0;
    const quarantineDir = await this.#ensureQuarantineRoot();
    const quarantineEntries = await readdir(quarantineDir, { withFileTypes: true });
    for (const entry of quarantineEntries) {
      const match = entry.isFile() ? EVENT_FILE.exec(entry.name) : null;
      if (match === null) {
        continue;
      }
      const record = await this.#readQuarantineRecord(match[1], quarantineDir);
      if (record.captured_at < cutoff) {
        await unlink(this.#target(match[1], quarantineDir));
        quarantine += 1;
      }
    }
    if (events > 0) {
      await syncDirectory(this.#rootDir);
    }
    if (quarantine > 0) {
      await syncDirectory(quarantineDir);
    }
    return { events, quarantine };
  }
}
