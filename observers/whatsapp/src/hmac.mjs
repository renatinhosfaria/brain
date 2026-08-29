import { createHmac } from 'node:crypto';

const CANONICAL_PHONE = /^[1-9][0-9]{6,14}$/;

function requireText(value, name, allowEmpty = false) {
  if (typeof value !== 'string' || (!allowEmpty && value.length === 0)) {
    throw new TypeError(`${name} must be a ${allowEmpty ? '' : 'non-empty '}string`);
  }
  return value;
}

function frame(domain, parts) {
  const framed = [Buffer.from(domain, 'ascii'), Buffer.from([0])];
  for (const [index, part] of parts.entries()) {
    const encoded = Buffer.from(requireText(part, `part_${index}`, true), 'utf8');
    const length = Buffer.alloc(8);
    length.writeBigUInt64BE(BigInt(encoded.length));
    framed.push(length, encoded);
  }
  return Buffer.concat(framed);
}

export class TransportIds {
  #secret;

  constructor(secret) {
    if (!(secret instanceof Uint8Array) || secret.byteLength < 32) {
      throw new TypeError('transport secret must contain at least 32 bytes');
    }
    this.#secret = Buffer.from(secret);
  }

  #digest(domain, ...parts) {
    return createHmac('sha256', this.#secret)
      .update(frame(domain, parts))
      .digest('hex');
  }

  eventId(observerDeviceId, observerMessageId) {
    const device = requireText(observerDeviceId, 'observerDeviceId');
    const message = requireText(observerMessageId, 'observerMessageId');
    return `waevt_${this.#digest('brain.transport.event.v1', device, message)}`;
  }

  contactKey(canonicalPhone) {
    const value = requireText(canonicalPhone, 'canonicalPhone');
    if (!CANONICAL_PHONE.test(value)) {
      throw new TypeError('canonical phone has invalid format');
    }
    return this.#digest('brain.transport.contact.v1', value);
  }

  bodyHmac(body) {
    return this.#digest(
      'brain.transport.body.v1',
      requireText(body, 'body', true),
    );
  }

  jidHmac(jid) {
    return this.#digest('brain.transport.jid.v1', requireText(jid, 'jid'));
  }

  opaqueHmac(value) {
    return this.#digest(
      'brain.transport.opaque.v1',
      requireText(value, 'value', true),
    );
  }
}
