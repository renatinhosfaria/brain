import datetime as dt
import hashlib
import hmac
import json

import httpx

from brain.storage import repositories


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    msg = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _serialize_payload(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _retry_delay(attempts: int) -> dt.timedelta:
    seconds = min(2 ** max(attempts - 1, 0), 300)
    return dt.timedelta(seconds=seconds)


async def deliver_once(session_factory, settings, *, worker_id="outbox", client=None) -> bool:
    webhook_url = settings.hermes_webhook_url
    if not webhook_url:
        return False

    async with session_factory() as session:
        now = dt.datetime.now(dt.UTC)
        event = await repositories.claim_next_outbox_event(
            session,
            now,
            worker_id=worker_id,
        )
        if event is None:
            return False
        event_id = event.id
        event_type = event.type
        payload = event.payload
        attempts = event.attempts
        await session.commit()

    body = _serialize_payload(payload)
    timestamp = dt.datetime.now(dt.UTC).isoformat()
    headers = {
        "Content-Type": "application/json",
        "X-Brain-Event-Id": str(event_id),
        "X-Brain-Event-Type": event_type,
        "X-Brain-Timestamp": timestamp,
        "X-Brain-Signature": sign_webhook(
            settings.hermes_webhook_secret or "",
            timestamp,
            body,
        ),
    }

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10)

    try:
        try:
            response = await client.post(webhook_url, content=body, headers=headers)
            error = None
            if not 200 <= response.status_code < 300:
                error = f"Hermes webhook returned HTTP {response.status_code}"
        except httpx.TransportError as exc:
            error = f"Hermes webhook transport error: {exc}"

        async with session_factory() as session:
            if error is None:
                await repositories.mark_outbox_delivered(session, event_id)
            elif attempts >= settings.outbox_max_attempts:
                await repositories.mark_outbox_failed(session, event_id, error=error)
            else:
                await repositories.mark_outbox_retrying(
                    session,
                    event_id,
                    error=error,
                    run_after=dt.datetime.now(dt.UTC) + _retry_delay(attempts),
                )
            await session.commit()
    finally:
        if close_client:
            await client.aclose()

    return True
