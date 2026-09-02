import assert from 'node:assert/strict';
import test from 'node:test';
import {
  RawAttributionError,
  encodeRawAttribution,
  validateEncodedRawAttribution,
} from '../src/raw-attribution.mjs';

function rejectsCode(action, code) {
  assert.throws(action, (error) => {
    assert.equal(error.code, code);
    assert.equal(error.name, 'RawAttributionError');
    assert.doesNotMatch(error.message, /synthetic|secret|fixture/i);
    return true;
  });
}

test('encodes nested, binary, integer, empty, and future values', () => {
  const encoded = encodeRawAttribution({
    sourceId: '123',
    title: '',
    flags: [true, false, null],
    thumbnail: Buffer.from([0, 1, 2, 255]),
    futureField: { integer: 9223372036854775807n },
  });
  assert.deepEqual(encoded.value.thumbnail, {
    $type: 'bytes', encoding: 'base64', data: 'AAEC/w==',
  });
  assert.deepEqual(encoded.value.futureField.integer, {
    $type: 'integer', encoding: 'decimal', data: '9223372036854775807',
  });
  assert.equal(validateEncodedRawAttribution(encoded.value), encoded.canonicalJson);
});

test('canonical JSON ignores input key order', () => {
  assert.equal(
    encodeRawAttribution({ z: 1, a: { y: 2, b: 3 } }).canonicalJson,
    encodeRawAttribution({ a: { b: 3, y: 2 }, z: 1 }).canonicalJson,
  );
});

test('rejects unsafe values with stable codes', () => {
  const cycle = {}; cycle.self = cycle;
  rejectsCode(() => encodeRawAttribution(cycle), 'raw_cycle');
  const accessor = {}; Object.defineProperty(accessor, 'secret', { get() { return 'fixture-secret'; } });
  rejectsCode(() => encodeRawAttribution(accessor), 'raw_accessor');
  rejectsCode(() => encodeRawAttribution('\ud800'), 'raw_unicode');
  for (const value of [NaN, Infinity, -Infinity, () => {}, Symbol('fixture-secret')]) {
    rejectsCode(() => encodeRawAttribution(value), 'raw_type');
  }
  const deep = 0; let nested = deep;
  for (let index = 0; index < 33; index += 1) nested = [nested];
  rejectsCode(() => encodeRawAttribution(nested), 'raw_depth');
  rejectsCode(() => encodeRawAttribution(Array.from({ length: 10001 }, () => null)), 'raw_nodes');
});

test('rejects invalid encoded tags and oversized output', () => {
  rejectsCode(() => validateEncodedRawAttribution({ $type: 'bytes', encoding: 'hex', data: '00' }), 'raw_tag');
  for (const data of ['A', 'AA=']) {
    rejectsCode(() => validateEncodedRawAttribution({ $type: 'bytes', encoding: 'base64', data }), 'raw_tag');
  }
  rejectsCode(() => encodeRawAttribution({ value: 'x'.repeat(4 * 1024 * 1024 + 1) }), 'raw_size');
});

test('preserves unsafe integer numbers as decimal tags', () => {
  assert.deepEqual(encodeRawAttribution(9007199254740992).value, {
    $type: 'integer', encoding: 'decimal', data: '9007199254740992',
  });
});

test('preserves an own __proto__ field', () => {
  const input = {};
  Object.defineProperty(input, '__proto__', { value: { kept: true }, enumerable: true });
  const encoded = encodeRawAttribution(input).value;
  assert.equal(Object.prototype.hasOwnProperty.call(encoded, '__proto__'), true);
  assert.deepEqual(encoded['__proto__'], { kept: true });
});

test('rejects malformed array index keys', () => {
  const malformed = [null];
  Object.defineProperty(malformed, '00', { value: null, enumerable: true });
  rejectsCode(() => encodeRawAttribution(malformed), 'raw_type');
  rejectsCode(() => validateEncodedRawAttribution(malformed), 'raw_tag');
});

test('error class is exported', () => {
  assert.equal(new RawAttributionError('raw_type').code, 'raw_type');
});
