import {
  DEFAULT_RAW_ATTRIBUTION_LIMITS,
  validateEncodedRawAttribution,
} from './raw-attribution.mjs';

const EVENT_ID = /^waevt_[0-9a-f]{64}$/;
const ROUTE = '/internal/transport/events';
const MAX_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_REQUEST_BYTES = 5 * 1024 * 1024;
const EVENT_FIELDS = new Set([
  'observer_event_version',
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
  constructor(message, { code, retryable, status = null, brainError = null }) {
    super(message);
    this.name = 'BrainClientError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
    // Codigo de erro do Brain, quando ele mandou um reconhecivel. E o unico
    // pedaco da resposta que atravessa: um enum curto, nunca o corpo.
    this.brainError = brainError;
  }
}

// O Brain responde erro como {"error":"CODIGO"}, de um conjunto fechado.
// Sem isto o motivo da recusa e descartado e uma falha definitiva vira um
// numero solto -- foi assim que um 400 recorrente ficou 41h sem diagnostico.
const BRAIN_ERROR_CODE = /^[A-Z][A-Z0-9_]{0,63}$/;
const MAX_ERROR_BODY_BYTES = 4096;

async function readBrainErrorCode(response) {
  try {
    const declared = Number(response.headers?.get?.('content-length'));
    if (Number.isFinite(declared) && declared > MAX_ERROR_BODY_BYTES) {
      return null;
    }
    const text = (await response.text()).slice(0, MAX_ERROR_BODY_BYTES);
    const code = JSON.parse(text)?.error;
    // Vale como codigo so o que casa com o formato do enum: um corpo
    // inesperado (proxy, HTML de erro) nao entra no log como texto livre.
    return typeof code === 'string' && BRAIN_ERROR_CODE.test(code) ? code : null;
  } catch {
    return null;
  }
}

function invalidPayload() {
  throw new TypeError('Brain event payload is invalid');
}

function requestPayload(safeEvent, maximumBytes, rawLimits) {
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
    invalidPayload();
  }
  const isV2 = safeEvent.observer_event_version === 2;
  const hasVersion = Object.hasOwn(safeEvent, 'observer_event_version');
  const hasExternal = Object.hasOwn(safeEvent, 'external_ad_reply');
  const hasRaw = Object.hasOwn(safeEvent, 'external_ad_reply_raw');
  if (
    (hasVersion && !isV2) ||
    (!isV2 && hasRaw) ||
    (isV2 && hasExternal !== hasRaw)
  ) {
    invalidPayload();
  }
  if (hasRaw) {
    try {
      const rawDescriptor = Object.getOwnPropertyDescriptor(
        safeEvent,
        'external_ad_reply_raw',
      );
      if (rawDescriptor === undefined || !('value' in rawDescriptor)) {
        invalidPayload();
      }
      validateEncodedRawAttribution(rawDescriptor.value, rawLimits);
    } catch {
      invalidPayload();
    }
  }
  let body;
  try {
    body = JSON.stringify(safeEvent);
  } catch {
    invalidPayload();
  }
  if (typeof body !== 'string') {
    invalidPayload();
  }
  if (Buffer.byteLength(body, 'utf8') > maximumBytes) {
    throw new TypeError('Brain event payload exceeds request size limit');
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
  #maxRequestBytes;
  #rawLimits;
  #timeoutMs;
  #token;

  constructor({
    baseUrl,
    token,
    timeoutMs,
    fetchImpl = globalThis.fetch,
    maxRequestBytes = DEFAULT_MAX_REQUEST_BYTES,
    rawLimits = DEFAULT_RAW_ATTRIBUTION_LIMITS,
  }) {
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
    if (!Number.isSafeInteger(maxRequestBytes) || maxRequestBytes <= 0) {
      throw new TypeError('Brain request size limit is invalid');
    }
    try {
      validateEncodedRawAttribution(null, rawLimits);
    } catch {
      throw new TypeError('Brain raw attribution limits are invalid');
    }
    this.#endpoint = new URL(ROUTE, parsed);
    this.#fetch = fetchImpl;
    this.#maxRequestBytes = maxRequestBytes;
    this.#rawLimits = {
      maxBytes: rawLimits.maxBytes,
      maxDepth: rawLimits.maxDepth,
      maxNodes: rawLimits.maxNodes,
    };
    this.#timeoutMs = timeoutMs;
    this.#token = token;
  }

  async ingest(safeEvent) {
    const body = requestPayload(
      safeEvent,
      this.#maxRequestBytes,
      this.#rawLimits,
    );
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
          brainError: await readBrainErrorCode(response),
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
