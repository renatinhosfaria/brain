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


async def _post_event(
    webhook_url: str,
    webhook_secret: str,
    event_id,
    event_type: str,
    payload: dict,
    client,
) -> str | None:
    body = _serialize_payload(payload)
    timestamp = dt.datetime.now(dt.UTC).isoformat()
    headers = {
        "Content-Type": "application/json",
        "X-Brain-Event-Id": str(event_id),
        "X-Brain-Event-Type": event_type,
        "X-Brain-Timestamp": timestamp,
        "X-Brain-Signature": sign_webhook(webhook_secret, timestamp, body),
    }

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10)

    try:
        response = await client.post(webhook_url, content=body, headers=headers)
    finally:
        if close_client:
            await client.aclose()

    if 200 <= response.status_code < 300:
        return None
    return f"Hermes webhook returned HTTP {response.status_code}"


async def deliver_once(session_factory, settings, *, worker_id="outbox", client=None) -> bool:
    webhook_url = settings.hermes_webhook_url
    if not webhook_url:
        return False
    webhook_secret = (settings.hermes_webhook_secret or "").strip()
    if not webhook_secret:
        return False

    async with session_factory() as session:
        now = dt.datetime.now(dt.UTC)
        event = await repositories.claim_next_outbox_event(
            session,
            now,
            worker_id=worker_id,
            stale_before=now - dt.timedelta(seconds=settings.job_stale_seconds),
        )
        if event is None:
            return False
        event_id = event.id
        event_type = event.type
        payload = event.payload
        attempts = event.attempts
        claim = repositories.outbox_claim_token(event)
        await session.commit()

    try:
        error = await _post_event(
            webhook_url,
            webhook_secret,
            event_id,
            event_type,
            payload,
            client,
        )
    except httpx.TransportError as exc:
        error = f"Hermes webhook transport error: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"Hermes webhook delivery error: {exc}"

    async with session_factory() as session:
        if error is None:
            await repositories.mark_outbox_delivered(session, event_id, claim=claim)
        elif attempts >= settings.outbox_max_attempts:
            await repositories.mark_outbox_failed(
                session,
                event_id,
                error=error,
                claim=claim,
            )
        else:
            await repositories.mark_outbox_retrying(
                session,
                event_id,
                error=error,
                run_after=dt.datetime.now(dt.UTC) + _retry_delay(attempts),
                claim=claim,
            )
        await session.commit()

    return True
