import { createServer } from 'node:http';

const WHATSAPP_STATES = new Set([
  'connected',
  'connecting',
  'disconnected',
]);

function nonnegativeInteger(value, name) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a non-negative integer`);
  }
  return value;
}

export class HealthState {
  #fatal = false;
  #now;
  #permanentFailureCount = 0;
  #purgedEventCount = 0;
  #rawCaptureFailureCount = 0;
  #retryPending = false;
  #spool;
  #unresolvedIdentityCount = 0;
  #whatsapp = 'connecting';

  constructor({ spool, now = () => Date.now() / 1000 }) {
    if (
      spool === null ||
      typeof spool !== 'object' ||
      typeof spool.list !== 'function' ||
      typeof spool.read !== 'function' ||
      typeof now !== 'function'
    ) {
      throw new TypeError('health dependencies are invalid');
    }
    this.#spool = spool;
    this.#now = now;
  }

  setWhatsApp(value) {
    if (!WHATSAPP_STATES.has(value)) {
      throw new TypeError('WhatsApp health state is invalid');
    }
    this.#whatsapp = value;
  }

  setRetryPending(value) {
    if (typeof value !== 'boolean') {
      throw new TypeError('retry state is invalid');
    }
    this.#retryPending = value;
  }

  setFatal(value) {
    if (typeof value !== 'boolean') {
      throw new TypeError('fatal state is invalid');
    }
    this.#fatal = value;
  }

  incrementUnresolvedIdentity() {
    this.#unresolvedIdentityCount += 1;
  }

  incrementPermanentFailure() {
    this.#permanentFailureCount += 1;
  }

  incrementRawCaptureFailure() {
    this.#rawCaptureFailureCount += 1;
  }

  addPurgedEvents(count) {
    this.#purgedEventCount += nonnegativeInteger(count, 'purged count');
  }

  async snapshot() {
    let eventIds;
    let oldestAge = 0;
    try {
      const now = this.#now();
      if (!Number.isFinite(now) || now <= 0) {
        throw new Error('health clock is invalid');
      }
      eventIds = await this.#spool.list();
      if (!Array.isArray(eventIds)) {
        throw new Error('outbox listing is invalid');
      }
      for (const eventId of eventIds) {
        const event = await this.#spool.read(eventId);
        if (!Number.isFinite(event?.received_at) || event.received_at <= 0) {
          throw new Error('outbox event timestamp is invalid');
        }
        oldestAge = Math.max(oldestAge, Math.floor(Math.max(0, now - event.received_at)));
      }
    } catch {
      return {
        status: 'unavailable',
        whatsapp: this.#whatsapp,
        outbox_depth: 0,
        outbox_oldest_age_seconds: 0,
        unresolved_identity_count: this.#unresolvedIdentityCount,
        permanent_failure_count: this.#permanentFailureCount,
        raw_capture_failure_count: this.#rawCaptureFailureCount,
        purged_event_count: this.#purgedEventCount,
      };
    }

    const unavailable = this.#fatal || this.#whatsapp === 'disconnected';
    const degraded =
      this.#whatsapp === 'connecting' ||
      this.#retryPending ||
      eventIds.length > 0 ||
      this.#permanentFailureCount > 0 ||
      this.#rawCaptureFailureCount > 0;
    return {
      status: unavailable ? 'unavailable' : degraded ? 'degraded' : 'ok',
      whatsapp: this.#whatsapp,
      outbox_depth: eventIds.length,
      outbox_oldest_age_seconds: oldestAge,
      unresolved_identity_count: this.#unresolvedIdentityCount,
      permanent_failure_count: this.#permanentFailureCount,
      raw_capture_failure_count: this.#rawCaptureFailureCount,
      purged_event_count: this.#purgedEventCount,
    };
  }
}

export function createHealthServer({
  host = '127.0.0.1',
  port = 8775,
  snapshot,
}) {
  if (host !== '127.0.0.1') {
    throw new TypeError('health must bind only to 127.0.0.1 loopback');
  }
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new TypeError('health port is invalid');
  }
  if (typeof snapshot !== 'function') {
    throw new TypeError('health snapshot provider is invalid');
  }

  const server = createServer(async (request, response) => {
    const pathname = new URL(request.url ?? '/', 'http://127.0.0.1').pathname;
    if (request.method !== 'GET' || pathname !== '/health') {
      response.writeHead(404, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: 'NOT_FOUND' }));
      return;
    }

    let payload;
    try {
      payload = await snapshot();
    } catch {
      payload = {
        status: 'unavailable',
        whatsapp: 'disconnected',
        outbox_depth: 0,
        outbox_oldest_age_seconds: 0,
        unresolved_identity_count: 0,
        permanent_failure_count: 0,
        raw_capture_failure_count: 0,
        purged_event_count: 0,
      };
    }
    response.writeHead(payload.status === 'unavailable' ? 503 : 200, {
      'content-type': 'application/json',
      'cache-control': 'no-store',
    });
    response.end(JSON.stringify(payload));
  });

  return {
    address() {
      return server.address();
    },
    async close() {
      if (!server.listening) {
        return;
      }
      await new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      });
    },
    async start() {
      if (server.listening) {
        return server.address();
      }
      await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen({ host, port }, resolve);
      });
      return server.address();
    },
  };
}
