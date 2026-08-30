import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { BrainClient } from './brain-client.mjs';
import { createHealthServer, HealthState } from './health.mjs';
import { TransportIds } from './hmac.mjs';
import {
  deriveIdentityEvidence as defaultDeriveIdentityEvidence,
  persistLidMapping as defaultPersistLidMapping,
} from './identity.mjs';
import { normalizeInboundMessage } from './normalize.mjs';
import { SafeSpool } from './spool.mjs';

const OBSERVER_ROOT = '/var/lib/brain/whatsapp-observer';
const RETENTION_SECONDS = 72 * 60 * 60;
const DEFAULT_RETRY_BASE_MS = 1_000;
const DEFAULT_RETRY_MAX_MS = 60_000;
const DEFAULT_RETRY_ATTEMPTS = 6;

function requiredText(value, name) {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new TypeError(`${name} is invalid`);
  }
  return value;
}

function integerSetting(value, name, { minimum, maximum }) {
  const parsed = typeof value === 'string' ? Number(value) : value;
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new TypeError(`${name} is invalid`);
  }
  return parsed;
}

function insideObserverRoot(value, name) {
  const resolved = path.resolve(requiredText(value, name));
  const relative = path.relative(OBSERVER_ROOT, resolved);
  if (
    resolved === OBSERVER_ROOT ||
    relative === '' ||
    relative.startsWith('..') ||
    path.isAbsolute(relative)
  ) {
    throw new TypeError(`${name} must use the private observer subtree`);
  }
  return resolved;
}

function decodeTransportSecret(value) {
  const text = requiredText(value, 'BRAIN_TRANSPORT_HMAC_SECRET');
  const secret = /^[0-9a-f]{64}$/u.test(text)
    ? Buffer.from(text, 'hex')
    : Buffer.from(text, 'utf8');
  if (secret.byteLength < 32) {
    throw new TypeError('BRAIN_TRANSPORT_HMAC_SECRET is invalid');
  }
  return secret;
}

export function loadObserverConfig(env = process.env) {
  const sessionDir = insideObserverRoot(
    env.BRAIN_OBSERVER_SESSION_DIR,
    'observer session directory',
  );
  const outboxDir = insideObserverRoot(
    env.BRAIN_OBSERVER_OUTBOX_DIR,
    'observer outbox directory',
  );
  if (sessionDir === outboxDir) {
    throw new TypeError('observer session and outbox directories must differ');
  }
  const healthHost = env.BRAIN_OBSERVER_HEALTH_HOST ?? '127.0.0.1';
  if (healthHost !== '127.0.0.1') {
    throw new TypeError('observer health host must be 127.0.0.1');
  }
  const healthPort = integerSetting(
    env.BRAIN_OBSERVER_HEALTH_PORT ?? '8775',
    'observer health port',
    { minimum: 1, maximum: 65_535 },
  );
  const brainUrl = requiredText(env.BRAIN_URL, 'BRAIN_URL');
  const parsedBrainUrl = new URL(brainUrl);
  if (
    parsedBrainUrl.protocol !== 'http:' ||
    parsedBrainUrl.hostname !== '127.0.0.1'
  ) {
    throw new TypeError('BRAIN_URL must use localhost HTTP');
  }
  return {
    brainUrl,
    deviceId: requiredText(
      env.BRAIN_OBSERVER_DEVICE_ID,
      'BRAIN_OBSERVER_DEVICE_ID',
    ),
    healthHost,
    healthPort,
    observerToken: requiredText(
      env.BRAIN_OBSERVER_TOKEN,
      'BRAIN_OBSERVER_TOKEN',
    ),
    outboxDir,
    sessionDir,
    transportSecret: decodeTransportSecret(env.BRAIN_TRANSPORT_HMAC_SECRET),
  };
}

function abortableDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    const abort = () => {
      clearTimeout(timer);
      const error = new Error('observer wait cancelled');
      error.name = 'AbortError';
      reject(error);
    };
    if (signal.aborted) {
      abort();
      return;
    }
    signal.addEventListener('abort', abort, { once: true });
  });
}

function disconnectCode(update) {
  const error = update?.lastDisconnect?.error;
  for (const value of [
    error?.output?.statusCode,
    error?.statusCode,
    error?.data?.statusCode,
  ]) {
    if (Number.isSafeInteger(value)) {
      return value;
    }
  }
  return null;
}

function supportedInbound(message) {
  if (
    message === null ||
    typeof message !== 'object' ||
    message.key?.fromMe === true ||
    typeof message.key?.remoteJid !== 'string' ||
    message.key.remoteJid.endsWith('@g.us')
  ) {
    return false;
  }
  return (
    (typeof message.message?.conversation === 'string' &&
      message.message.conversation.length > 0) ||
    (typeof message.message?.extendedTextMessage?.text === 'string' &&
      message.message.extendedTextMessage.text.length > 0)
  );
}

function validateRuntimeOptions(options) {
  const functionNames = [
    'makeSocket',
    'normalize',
    'deriveIdentityEvidence',
    'persistLidMapping',
    'renderQr',
    'now',
    'sleep',
  ];
  for (const name of functionNames) {
    if (typeof options[name] !== 'function') {
      throw new TypeError(`observer runtime ${name} dependency is invalid`);
    }
  }
  for (const [objectName, methods] of [
    ['authState', ['saveCreds']],
    ['spool', ['put', 'list', 'read', 'ack', 'expireDisplayNames', 'purgeOlderThan']],
    ['client', ['ingest']],
    [
      'healthState',
      [
        'setWhatsApp',
        'setRetryPending',
        'setFatal',
        'incrementUnresolvedIdentity',
        'incrementPermanentFailure',
        'addPurgedEvents',
      ],
    ],
  ]) {
    if (options[objectName] === null || typeof options[objectName] !== 'object') {
      throw new TypeError(`observer runtime ${objectName} dependency is invalid`);
    }
    for (const method of methods) {
      if (typeof options[objectName][method] !== 'function') {
        throw new TypeError(`observer runtime ${objectName} dependency is invalid`);
      }
    }
  }
  requiredText(options.observerSessionDir, 'observerSessionDir');
  requiredText(options.observerDeviceId, 'observerDeviceId');
}

export async function runObserver({
  makeSocket,
  authState,
  ids,
  normalize,
  deriveIdentityEvidence,
  persistLidMapping,
  spool,
  client,
  healthState,
  healthServer = null,
  observerSessionDir,
  observerDeviceId,
  renderQr,
  disconnectReasons,
  now,
  sleep,
  retryBaseMs = DEFAULT_RETRY_BASE_MS,
  retryMaxMs = DEFAULT_RETRY_MAX_MS,
  retryMaxAttempts = DEFAULT_RETRY_ATTEMPTS,
  reconnectMaxAttempts = DEFAULT_RETRY_ATTEMPTS,
}) {
  const options = {
    authState,
    client,
    deriveIdentityEvidence,
    healthState,
    makeSocket,
    normalize,
    now,
    persistLidMapping,
    renderQr,
    sleep,
    spool,
  };
  validateRuntimeOptions({
    ...options,
    observerDeviceId,
    observerSessionDir,
  });
  const retryBase = integerSetting(retryBaseMs, 'retryBaseMs', {
    minimum: 1,
    maximum: 60_000,
  });
  const retryMaximum = integerSetting(retryMaxMs, 'retryMaxMs', {
    minimum: retryBase,
    maximum: 300_000,
  });
  const deliveryAttempts = integerSetting(
    retryMaxAttempts,
    'retryMaxAttempts',
    { minimum: 0, maximum: 100 },
  );
  const reconnectAttemptsMaximum = integerSetting(
    reconnectMaxAttempts,
    'reconnectMaxAttempts',
    { minimum: 0, maximum: 100 },
  );
  const reasons = disconnectReasons ?? {};
  const fatalReasons = new Set([
    reasons.loggedOut ?? 401,
    reasons.connectionReplaced ?? 440,
    reasons.badSession ?? 500,
    reasons.multideviceMismatch ?? 411,
    reasons.forbidden ?? 403,
  ]);
  const cancellation = new AbortController();
  const permanentlySuppressed = new Set();
  let stopped = false;
  let socket = null;
  let listeners = null;
  let reconnectAttempts = 0;
  let reconnectPromise = null;
  let retryPromise = null;
  let queue = Promise.resolve();
  let activeMessageCallbacks = 0;
  const messageIdleWaiters = new Set();

  function enqueue(task) {
    const result = queue.then(() => (stopped ? undefined : task()));
    queue = result.catch(() => {});
    return result;
  }

  function detachSocket() {
    if (socket?.ev && listeners) {
      socket.ev.off('creds.update', listeners.credentials);
      socket.ev.off('connection.update', listeners.connection);
      socket.ev.off('messages.upsert', listeners.messages);
    }
    listeners = null;
  }

  function waitForMessageCallbacks() {
    if (activeMessageCallbacks === 0) {
      return Promise.resolve();
    }
    return new Promise((resolve) => messageIdleWaiters.add(resolve));
  }

  function finishMessageCallback() {
    activeMessageCallbacks -= 1;
    if (activeMessageCallbacks === 0) {
      for (const resolve of messageIdleWaiters) {
        resolve();
      }
      messageIdleWaiters.clear();
    }
  }

  async function deliver(event) {
    if (permanentlySuppressed.has(event.event_id)) {
      return 'permanent';
    }
    try {
      await client.ingest(event);
      await spool.ack(event.event_id);
      return 'success';
    } catch (error) {
      if (error?.retryable === true) {
        return 'retryable';
      }
      permanentlySuppressed.add(event.event_id);
      healthState.incrementPermanentFailure();
      return 'permanent';
    }
  }

  async function maintainAndDrain() {
    const current = now();
    if (!Number.isFinite(current) || current <= 0) {
      throw new Error('observer runtime clock is invalid');
    }
    await spool.expireDisplayNames(current);
    const purged = await spool.purgeOlderThan(current - RETENTION_SECONDS);
    healthState.addPurgedEvents(purged);
    const eventIds = await spool.list();
    const pending = [];
    for (const eventId of eventIds) {
      pending.push(await spool.read(eventId));
    }
    pending.sort(
      (left, right) =>
        left.received_at - right.received_at ||
        left.event_id.localeCompare(right.event_id),
    );

    let retryable = false;
    for (const event of pending) {
      const outcome = await deliver(event);
      retryable ||= outcome === 'retryable';
    }
    healthState.setRetryPending(retryable);
    return retryable;
  }

  function startRetryLoop() {
    if (stopped || deliveryAttempts === 0 || retryPromise !== null) {
      return;
    }
    healthState.setRetryPending(true);
    retryPromise = (async () => {
      for (let attempt = 0; attempt < deliveryAttempts && !stopped; attempt += 1) {
        const delay = Math.min(retryBase * 2 ** attempt, retryMaximum);
        await sleep(delay, cancellation.signal);
        if (stopped) {
          return;
        }
        const remainsRetryable = await enqueue(maintainAndDrain);
        if (!remainsRetryable) {
          return;
        }
      }
    })();
    const currentRetry = retryPromise;
    void currentRetry.then(
      () => {
        if (retryPromise === currentRetry) {
          retryPromise = null;
        }
      },
      (error) => {
        if (retryPromise === currentRetry) {
          retryPromise = null;
        }
        if (error?.name !== 'AbortError' && !stopped) {
          healthState.setFatal(true);
        }
      },
    );
  }

  async function processMessage(message) {
    if (!supportedInbound(message)) {
      return;
    }
    const evidence = deriveIdentityEvidence(message);
    if (evidence === null) {
      return;
    }
    let mappingStatus = null;
    if (evidence.lid !== null && evidence.phoneJid !== null) {
      const mapping = await persistLidMapping(observerSessionDir, evidence);
      mappingStatus = mapping?.status ?? 'retryable';
    }
    const safeEvent = normalize(message, now(), ids, observerDeviceId);
    if (safeEvent === null) {
      return;
    }
    if (
      mappingStatus !== null &&
      !['written', 'unchanged'].includes(mappingStatus)
    ) {
      delete safeEvent.contact_key;
    }
    if (!Object.hasOwn(safeEvent, 'contact_key')) {
      healthState.incrementUnresolvedIdentity();
    }
    await spool.put(safeEvent);
    const outcome = await deliver(safeEvent);
    if (outcome === 'retryable') {
      startRetryLoop();
    }
  }

  async function processUpsert(update) {
    const messages = Array.isArray(update?.messages) ? update.messages : [];
    for (const message of messages) {
      await processMessage(message);
    }
  }

  async function handleMessagesUpdate(update) {
    if (stopped) {
      return;
    }
    activeMessageCallbacks += 1;
    try {
      await processUpsert(update);
    } catch {
      healthState.setFatal(true);
    } finally {
      finishMessageCallback();
    }
  }

  function scheduleReconnect() {
    if (stopped || reconnectPromise !== null) {
      return;
    }
    if (reconnectAttempts >= reconnectAttemptsMaximum) {
      healthState.setFatal(true);
      return;
    }
    const delay = Math.min(retryBase * 2 ** reconnectAttempts, retryMaximum);
    reconnectAttempts += 1;
    reconnectPromise = (async () => {
      await sleep(delay, cancellation.signal);
      if (!stopped) {
        await connect();
      }
    })();
    const currentReconnect = reconnectPromise;
    void currentReconnect.then(
      () => {
        if (reconnectPromise === currentReconnect) {
          reconnectPromise = null;
        }
      },
      (error) => {
        if (reconnectPromise === currentReconnect) {
          reconnectPromise = null;
        }
        if (error?.name !== 'AbortError' && !stopped) {
          healthState.setFatal(true);
        }
      },
    );
  }

  async function handleConnectionUpdate(update) {
    if (typeof update?.qr === 'string' && update.qr.length > 0) {
      renderQr(update.qr);
    }
    if (update?.connection === 'connecting') {
      healthState.setWhatsApp('connecting');
      return;
    }
    if (update?.connection === 'open') {
      reconnectAttempts = 0;
      healthState.setFatal(false);
      healthState.setWhatsApp('connected');
      if (await maintainAndDrain()) {
        startRetryLoop();
      }
      return;
    }
    if (update?.connection !== 'close') {
      return;
    }
    healthState.setWhatsApp('disconnected');
    const code = disconnectCode(update);
    if (fatalReasons.has(code)) {
      healthState.setFatal(true);
      return;
    }
    scheduleReconnect();
  }

  async function connect() {
    if (stopped) {
      return;
    }
    detachSocket();
    healthState.setWhatsApp('connecting');
    socket = await makeSocket({ auth: authState.state });
    if (
      socket === null ||
      typeof socket !== 'object' ||
      socket.ev === null ||
      typeof socket.ev?.on !== 'function' ||
      typeof socket.ev?.off !== 'function'
    ) {
      throw new Error('observer socket is invalid');
    }
    listeners = {
      credentials: () => {
        void enqueue(() => authState.saveCreds()).catch(() => {
          healthState.setFatal(true);
        });
      },
      connection: (update) => {
        void enqueue(() => handleConnectionUpdate(update)).catch(() => {
          healthState.setFatal(true);
        });
      },
      messages: handleMessagesUpdate,
    };
    socket.ev.on('creds.update', listeners.credentials);
    socket.ev.on('connection.update', listeners.connection);
    socket.ev.on('messages.upsert', listeners.messages);
  }

  if (healthServer !== null) {
    if (
      typeof healthServer?.start !== 'function' ||
      typeof healthServer?.close !== 'function'
    ) {
      throw new TypeError('observer health server is invalid');
    }
    await healthServer.start();
  }
  if (await maintainAndDrain()) {
    startRetryLoop();
  }
  await connect();

  return {
    async close() {
      if (stopped) {
        return;
      }
      stopped = true;
      cancellation.abort();
      detachSocket();
      socket?.end?.(new Error('observer shutdown'));
      await Promise.allSettled([reconnectPromise, retryPromise].filter(Boolean));
      await waitForMessageCallbacks();
      await queue;
      healthState.setWhatsApp('disconnected');
      await healthServer?.close();
    },
    drain() {
      return enqueue(async () => {
        const retryable = await maintainAndDrain();
        if (retryable) {
          startRetryLoop();
        }
        return retryable;
      });
    },
    async idle() {
      await queue;
      await waitForMessageCallbacks();
    },
    async waitForBackground() {
      while (reconnectPromise !== null || retryPromise !== null) {
        await Promise.allSettled(
          [reconnectPromise, retryPromise].filter(Boolean),
        );
      }
    },
  };
}

export async function bootstrapObserver(env = process.env) {
  const config = loadObserverConfig(env);
  const baileys = await import('@whiskeysockets/baileys');
  const qrModule = await import('qrcode-terminal');
  const auth = await baileys.useMultiFileAuthState(config.sessionDir);
  const ids = new TransportIds(config.transportSecret);
  const spool = new SafeSpool(config.outboxDir);
  const client = new BrainClient({
    baseUrl: config.brainUrl,
    token: config.observerToken,
    timeoutMs: 10_000,
  });
  const healthState = new HealthState({ spool });
  const healthServer = createHealthServer({
    host: config.healthHost,
    port: config.healthPort,
    snapshot: () => healthState.snapshot(),
  });
  const qr = qrModule.default ?? qrModule;
  return runObserver({
    authState: { state: auth.state, saveCreds: auth.saveCreds },
    client,
    deriveIdentityEvidence: defaultDeriveIdentityEvidence,
    disconnectReasons: baileys.DisconnectReason,
    healthServer,
    healthState,
    ids,
    makeSocket: ({ auth: observerAuth }) =>
      baileys.makeWASocket({
        auth: observerAuth,
        markOnlineOnConnect: false,
        printQRInTerminal: false,
        syncFullHistory: false,
      }),
    normalize: normalizeInboundMessage,
    now: () => Date.now() / 1000,
    observerDeviceId: config.deviceId,
    observerSessionDir: config.sessionDir,
    persistLidMapping: defaultPersistLidMapping,
    renderQr: (value) => qr.generate(value, { small: true }),
    sleep: abortableDelay,
    spool,
  });
}

const directExecution =
  typeof process.argv[1] === 'string' &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;

if (directExecution) {
  void bootstrapObserver()
    .then((runtime) => {
      const shutdown = () => {
        void runtime.close();
      };
      process.once('SIGINT', shutdown);
      process.once('SIGTERM', shutdown);
    })
    .catch(() => {
      process.exitCode = 1;
    });
}
