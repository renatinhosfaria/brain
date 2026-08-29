const PHONE_JID = /^([1-9][0-9]{6,14})@s\.whatsapp\.net$/;
const LID_JID = /^[0-9]+@lid$/;
const OBSERVER_DEVICE_ID = /^[!-~]{1,128}$/;
const SAFE_HOSTNAME = /^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/gu;
const MAX_SAFE_LENGTH = 10_000_000;

function codePointLength(value) {
  return Array.from(value).length;
}

function boundedMetadata(value, maximum = 128) {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    CONTROL_CHARACTERS.test(value) ||
    codePointLength(value) > maximum
  ) {
    CONTROL_CHARACTERS.lastIndex = 0;
    return undefined;
  }
  CONTROL_CHARACTERS.lastIndex = 0;
  return value;
}

function sanitizedDisplayName(value) {
  if (typeof value !== 'string') {
    return undefined;
  }
  const sanitized = value.replace(CONTROL_CHARACTERS, '');
  const bounded = Array.from(sanitized).slice(0, 160).join('');
  return bounded || undefined;
}

function positiveTimestamp(value) {
  if (typeof value === 'number') {
    return Number.isFinite(value) && value > 0 ? value : undefined;
  }
  if (typeof value === 'bigint') {
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : undefined;
  }
  if (
    value !== null &&
    typeof value === 'object' &&
    Number.isInteger(value.low) &&
    Number.isInteger(value.high)
  ) {
    const number = (value.high >>> 0) * 2 ** 32 + (value.low >>> 0);
    return Number.isSafeInteger(number) && number > 0 ? number : undefined;
  }
  return undefined;
}

function safeOpaque(value, ids, prefix, target) {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    codePointLength(value) > MAX_SAFE_LENGTH
  ) {
    target[`${prefix}_present`] = false;
    return false;
  }
  target[`${prefix}_present`] = true;
  target[`${prefix}_length`] = codePointLength(value);
  target[`${prefix}_hmac`] = ids.opaqueHmac(value);
  return true;
}

function safeSourceUrl(value, ids, target) {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    codePointLength(value) > MAX_SAFE_LENGTH
  ) {
    return;
  }
  let hostname;
  try {
    hostname = new URL(value).hostname.toLowerCase();
  } catch {
    return;
  }
  if (!boundedMetadata(hostname, 253) || !SAFE_HOSTNAME.test(hostname)) {
    return;
  }
  target.source_url_hostname = hostname;
  target.source_url_length = codePointLength(value);
  target.source_url_hmac = ids.opaqueHmac(value);
}

function safeExternalAdReply(raw, ids) {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const safe = {};
  const sourceType = boundedMetadata(raw.sourceType);
  const sourceApp = boundedMetadata(raw.sourceApp);
  if (sourceType !== undefined) {
    safe.source_type = sourceType;
  }
  if (sourceApp !== undefined) {
    safe.source_app = sourceApp;
  }

  const sourceIdPresent = safeOpaque(raw.sourceId, ids, 'source_id', safe);
  const ctwaClidPresent = safeOpaque(raw.ctwaClid, ids, 'ctwa_clid', safe);
  safeSourceUrl(raw.sourceUrl, ids, safe);

  for (const [rawName, safeName] of [
    ['showAdAttribution', 'show_ad_attribution'],
    ['clickToWhatsappCall', 'click_to_whatsapp_call'],
    ['containsAutoReply', 'contains_auto_reply'],
  ]) {
    if (typeof raw[rawName] === 'boolean') {
      safe[safeName] = raw[rawName];
    }
  }

  return {
    safe,
    isCtwa:
      sourceType === 'ad' &&
      (raw.clickToWhatsappCall === true || sourceIdPresent || ctwaClidPresent),
  };
}

function provenText(message) {
  if (typeof message?.conversation === 'string' && message.conversation.length > 0) {
    return { body: message.conversation, nativeType: 'conversation', external: null };
  }
  const extended = message?.extendedTextMessage;
  if (typeof extended?.text === 'string' && extended.text.length > 0) {
    return {
      body: extended.text,
      nativeType: 'extendedTextMessage',
      external: extended.contextInfo?.externalAdReply ?? null,
    };
  }
  return null;
}

export function normalizeInboundMessage(msg, capturedAt, ids, observerDeviceId) {
  if (msg === null || typeof msg !== 'object' || msg.key?.fromMe === true) {
    return null;
  }
  const remoteJid = msg.key?.remoteJid;
  const observerMessageId = msg.key?.id;
  const phoneMatch = typeof remoteJid === 'string' ? PHONE_JID.exec(remoteJid) : null;
  const isLid = typeof remoteJid === 'string' && LID_JID.test(remoteJid);
  if (
    (!phoneMatch && !isLid) ||
    typeof observerMessageId !== 'string' ||
    observerMessageId.length === 0 ||
    typeof observerDeviceId !== 'string' ||
    !OBSERVER_DEVICE_ID.test(observerDeviceId) ||
    !Number.isFinite(capturedAt) ||
    capturedAt <= 0
  ) {
    return null;
  }

  const text = provenText(msg.message);
  if (text === null) {
    return null;
  }
  if (codePointLength(text.body) > MAX_SAFE_LENGTH) {
    return null;
  }
  const external = safeExternalAdReply(text.external, ids);
  const safe = {
    event_id: ids.eventId(observerDeviceId, observerMessageId),
    observer_device_id: observerDeviceId,
    received_at: capturedAt,
    remote_jid_hmac: ids.jidHmac(remoteJid),
    body_hmac: ids.bodyHmac(text.body),
    body_length: codePointLength(text.body),
    native_type: text.nativeType,
    transport_kind: external?.isCtwa ? 'ctwa_candidate' : 'ordinary_inbound',
  };

  const messageTimestamp = positiveTimestamp(msg.messageTimestamp);
  if (messageTimestamp !== undefined) {
    safe.message_timestamp = messageTimestamp;
  }
  if (phoneMatch) {
    safe.contact_key = ids.contactKey(phoneMatch[1]);
  }
  const displayName = sanitizedDisplayName(msg.pushName);
  if (displayName !== undefined) {
    safe.display_name = displayName;
  }
  if (external !== null) {
    safe.external_ad_reply = external.safe;
  }
  return safe;
}
