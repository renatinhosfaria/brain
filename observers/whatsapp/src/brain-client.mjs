const EVENT_ID = /^waevt_[0-9a-f]{64}$/;
const ROUTE = '/internal/transport/events';
const MAX_TIMEOUT_MS = 60_000;
const MAX_REQUEST_BYTES = 16_384;
const EVENT_FIELDS = new Set([
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
const SPOOL_ONLY_FIELDS = new Set([
  'spool_version',
  'spooled_at',
  'display_name_expires_at',
  'filename',
  'attempt',
  'attempt_count',
  'retry',
]);

export class BrainClientError extends Error {
  constructor(message, { code, retryable, status = null }) {
    super(message);
    this.name = 'BrainClientError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
}

function requestPayload(safeEvent) {
  if (
    safeEvent === null ||
    typeof safeEvent !== 'object' ||
    Array.isArray(safeEvent) ||
    typeof safeEvent.event_id !== 'string' ||
    !EVENT_ID.test(safeEvent.event_id) ||
    Object.keys(safeEvent).some(
      (field) => SPOOL_ONLY_FIELDS.has(field) || !EVENT_FIELDS.has(field),
    ) ||
    (Object.hasOwn(safeEvent, 'external_ad_reply') &&
      (safeEvent.external_ad_reply === null ||
        typeof safeEvent.external_ad_reply !== 'object' ||
        Array.isArray(safeEvent.external_ad_reply) ||
        Object.keys(safeEvent.external_ad_reply).some(
          (field) => !EXTERNAL_FIELDS.has(field),
        )))
  ) {
    throw new TypeError('Brain event payload is invalid');
  }
  let body;
  try {
    body = JSON.stringify(safeEvent);
  } catch {
    throw new TypeError('Brain event payload is invalid');
  }
  if (
    typeof body !== 'string' ||
    Buffer.byteLength(body, 'utf8') > MAX_REQUEST_BYTES
  ) {
    throw new TypeError('Brain event payload is invalid');
  }
  return body;
}

function responseIsExactSuccess(value, eventId) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).length === 3 &&
    value.status === 'ok' &&
    value.event_id === eventId &&
    typeof value.duplicate === 'boolean' &&
    Object.hasOwn(value, 'status') &&
    Object.hasOwn(value, 'event_id') &&
    Object.hasOwn(value, 'duplicate')
  );
}

function retryableStatus(status) {
  return [408, 425, 429].includes(status) || status >= 500;
}

export class BrainClient {
  #endpoint;
  #fetch;
  #timeoutMs;
  #token;

  constructor({ baseUrl, token, timeoutMs, fetchImpl = globalThis.fetch }) {
    let parsed;
    try {
      parsed = new URL(baseUrl);
    } catch {
      throw new TypeError('Brain base URL is invalid');
    }
    if (
      !['http:', 'https:'].includes(parsed.protocol) ||
      parsed.username !== '' ||
      parsed.password !== '' ||
      !['', '/'].includes(parsed.pathname) ||
      parsed.search !== '' ||
      parsed.hash !== ''
    ) {
      throw new TypeError('Brain base URL is invalid');
    }
    if (
      typeof token !== 'string' ||
      token.length === 0 ||
      token.length > 4096 ||
      /[\u0000-\u001f\u007f]/u.test(token)
    ) {
      throw new TypeError('Brain observer token is invalid');
    }
    if (
      !Number.isFinite(timeoutMs) ||
      timeoutMs <= 0 ||
      timeoutMs > MAX_TIMEOUT_MS
    ) {
      throw new TypeError('Brain client timeout is invalid');
    }
    if (typeof fetchImpl !== 'function') {
      throw new TypeError('Brain fetch implementation is invalid');
    }
    this.#endpoint = new URL(ROUTE, parsed);
    this.#fetch = fetchImpl;
    this.#timeoutMs = timeoutMs;
    this.#token = token;
  }

  async ingest(safeEvent) {
    const body = requestPayload(safeEvent);
    const eventId = safeEvent.event_id;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      let response;
      try {
        response = await this.#fetch(this.#endpoint, {
          method: 'POST',
          headers: {
            authorization: `Bearer ${this.#token}`,
            'content-type': 'application/json',
          },
          body,
          signal: controller.signal,
          redirect: 'error',
        });
      } catch {
        throw new BrainClientError('Brain ingest request failed', {
          code: 'NETWORK_ERROR',
          retryable: true,
          status: null,
        });
      }

      if (response.status !== 200) {
        throw new BrainClientError('Brain ingest request failed', {
          code: 'HTTP_ERROR',
          retryable: retryableStatus(response.status),
          status: response.status,
        });
      }

      let payload;
      try {
        payload = await response.json();
      } catch {
        throw new BrainClientError(
          controller.signal.aborted
            ? 'Brain ingest request failed'
            : 'Brain ingest protocol failure',
          {
            code: controller.signal.aborted ? 'NETWORK_ERROR' : 'PROTOCOL_ERROR',
            retryable: controller.signal.aborted,
            status: controller.signal.aborted ? null : response.status,
          },
        );
      }
      if (!responseIsExactSuccess(payload, eventId)) {
        throw new BrainClientError('Brain ingest protocol failure', {
          code: 'PROTOCOL_ERROR',
          retryable: false,
          status: response.status,
        });
      }
      return { event_id: payload.event_id, duplicate: payload.duplicate };
    } finally {
      clearTimeout(timeout);
    }
  }
}
