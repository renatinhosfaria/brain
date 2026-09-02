export const DEFAULT_RAW_ATTRIBUTION_LIMITS = Object.freeze({
  maxBytes: 4 * 1024 * 1024,
  maxDepth: 32,
  maxNodes: 10_000,
});

export class RawAttributionError extends Error {
  constructor(code) {
    super(code);
    this.name = 'RawAttributionError';
    this.code = code;
  }
}

const fail = (code) => { throw new RawAttributionError(code); };
const isObject = (value) => value !== null && typeof value === 'object';
const define = (object, key, value) => {
  Object.defineProperty(object, key, { value, enumerable: true, writable: true, configurable: true });
};

function checkedLimits(limits) {
  if (limits === null || typeof limits !== 'object') fail('raw_type');
  const checked = {};
  for (const name of ['maxBytes', 'maxDepth', 'maxNodes']) {
    const descriptor = Object.getOwnPropertyDescriptor(limits, name);
    if (!descriptor || !('value' in descriptor) ||
        !Number.isSafeInteger(descriptor.value) || descriptor.value <= 0) fail('raw_type');
    checked[name] = descriptor.value;
  }
  return checked;
}

function checkString(value) {
  if (/[\uD800-\uDFFF]/u.test(value)) fail('raw_unicode');
  return value;
}

function decimalNumber(value) {
  const text = value.toString(10);
  const exponentAt = text.search(/[eE]/);
  if (exponentAt < 0) return text;
  const exponent = Number(text.slice(exponentAt + 1));
  const sign = text.startsWith('-') ? '-' : '';
  const unsigned = sign ? text.slice(1, exponentAt) : text.slice(0, exponentAt);
  const dot = unsigned.indexOf('.');
  const digits = unsigned.replace('.', '');
  const decimalPosition = (dot < 0 ? unsigned.length : dot) + exponent;
  if (decimalPosition <= 0) return sign + '0.' + '0'.repeat(-decimalPosition) + digits;
  if (decimalPosition >= digits.length) return sign + digits + '0'.repeat(decimalPosition - digits.length);
  return sign + digits.slice(0, decimalPosition) + '.' + digits.slice(decimalPosition);
}

function ownEntries(value) {
  let keys;
  try { keys = Reflect.ownKeys(value); } catch { fail('raw_accessor'); }
  const entries = [];
  for (const key of keys) {
    if (typeof key !== 'string') fail('raw_type');
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !('value' in descriptor)) fail('raw_accessor');
    checkString(key);
    entries.push([key, descriptor.value]);
  }
  entries.sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  return entries;
}

function encodeValue(value, depth, state) {
  if (depth > state.limits.maxDepth) fail('raw_depth');
  state.nodes += 1;
  if (state.nodes > state.limits.maxNodes) fail('raw_nodes');
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'string') return checkString(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || Object.is(value, -0)) fail('raw_type');
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
      return { $type: 'integer', encoding: 'decimal', data: decimalNumber(value) };
    }
    return value;
  }
  if (typeof value === 'bigint') return { $type: 'integer', encoding: 'decimal', data: value.toString(10) };
  if (!isObject(value)) fail('raw_type');
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    let data;
    try { data = Buffer.from(value).toString('base64'); } catch { fail('raw_type'); }
    return { $type: 'bytes', encoding: 'base64', data };
  }
  if (state.seen.has(value)) fail('raw_cycle');
  state.seen.add(value);
  try {
    if (Array.isArray(value)) {
      if (Object.getPrototypeOf(value) !== Array.prototype) fail('raw_type');
      const entries = ownEntries(value).filter(([key]) => key !== 'length');
      const out = [];
      for (let index = 0; index < value.length; index += 1) {
        const entry = entries.find(([key]) => key === String(index));
        if (!entry) fail('raw_accessor');
        out.push(encodeValue(entry[1], depth + 1, state));
      }
      if (entries.length !== value.length ||
          entries.some(([key], index) => key !== String(index))) fail('raw_type');
      return out;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) fail('raw_type');
    const out = {};
    for (const [key, child] of ownEntries(value)) define(out, key, encodeValue(child, depth + 1, state));
    return out;
  } finally { state.seen.delete(value); }
}

export function encodeRawAttribution(value, limits = DEFAULT_RAW_ATTRIBUTION_LIMITS) {
  const state = { limits: checkedLimits(limits), nodes: 0, seen: new Set() };
  const encoded = encodeValue(value, 0, state);
  const canonicalJson = JSON.stringify(encoded);
  if (Buffer.byteLength(canonicalJson, 'utf8') > state.limits.maxBytes) fail('raw_size');
  return { value: encoded, canonicalJson };
}

function validateValue(value, depth, state) {
  if (depth > state.limits.maxDepth) fail('raw_depth');
  state.nodes += 1;
  if (state.nodes > state.limits.maxNodes) fail('raw_nodes');
  if (value === null || typeof value === 'boolean') return;
  if (typeof value === 'string') { checkString(value); return; }
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)) || Object.is(value, -0)) fail('raw_type');
    return;
  }
  if (!isObject(value)) fail('raw_type');
  if (state.seen.has(value)) fail('raw_cycle');
  state.seen.add(value);
  try {
    if (Array.isArray(value)) {
      const entries = ownEntries(value).filter(([key]) => key !== 'length');
      if (Object.getPrototypeOf(value) !== Array.prototype || entries.length !== value.length ||
          entries.some(([key], index) => key !== String(index))) fail('raw_tag');
      for (const [, child] of entries) validateValue(child, depth + 1, state);
      return;
    }
    const entries = ownEntries(value);
    const tag = entries.find(([key]) => key === '$type');
    if (tag) {
      if (entries.length !== 3 || (tag[1] !== 'bytes' && tag[1] !== 'integer')) fail('raw_tag');
      const encoding = entries.find(([key]) => key === 'encoding')?.[1];
      const data = entries.find(([key]) => key === 'data')?.[1];
      if (tag[1] === 'bytes' && encoding === 'base64' && typeof data === 'string' &&
          data.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(data) &&
          Buffer.from(data, 'base64').toString('base64') === data) return;
      if (tag[1] === 'integer' && encoding === 'decimal' && typeof data === 'string' && /^-?(?:0|[1-9]\d*)$/.test(data)) return;
      fail('raw_tag');
    }
    for (const [key, child] of entries) validateValue(child, depth + 1, state);
  } finally { state.seen.delete(value); }
}

function canonicalValue(value) {
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) {
    return ownEntries(value).filter(([key]) => key !== 'length').map(([, child]) => canonicalValue(child));
  }
  const entries = ownEntries(value);
  const tag = entries.find(([key]) => key === '$type');
  if (tag) {
    const encoding = entries.find(([key]) => key === 'encoding')[1];
    const data = entries.find(([key]) => key === 'data')[1];
    return tag[1] === 'bytes'
      ? { $type: 'bytes', encoding, data }
      : { $type: 'integer', encoding, data };
  }
  const out = {};
  for (const [key, child] of entries) define(out, key, canonicalValue(child));
  return out;
}

export function validateEncodedRawAttribution(value, limits = DEFAULT_RAW_ATTRIBUTION_LIMITS) {
  const state = { limits: checkedLimits(limits), nodes: 0, seen: new Set() };
  validateValue(value, 0, state);
  const canonicalJson = JSON.stringify(canonicalValue(value));
  if (Buffer.byteLength(canonicalJson, 'utf8') > state.limits.maxBytes) fail('raw_size');
  return canonicalJson;
}
