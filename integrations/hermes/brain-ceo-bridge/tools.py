"""Minimal CEO-side transport for the private Brain gateway route."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

ENDPOINT = "http://127.0.0.1:8765/internal/gateway/conversation-phone"
_SESSION_FIELDS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PROFILE",
)
_REQUIRED_SESSION_FIELDS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
)
_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
_MAX_RESPONSE_BYTES = 16_384
_HTTP_TIMEOUT_SECONDS = 5.0


def _unavailable() -> str:
    return json.dumps(
        {"status": "unavailable", "reason": "phone_not_resolved"},
        separators=(",", ":"),
    )


def _safe_context_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and all(0x21 <= ord(char) <= 0x7E for char in value)
        and "${" not in value
    )


def _session_context() -> dict[str, str] | None:
    # Import at call time so the active gateway ContextVar is read for every
    # tool invocation, including invocations in concurrent gateway tasks.
    from gateway.session_context import get_session_env

    values = {name: get_session_env(name, "") for name in _SESSION_FIELDS}
    if any(not _safe_context_value(values[name]) for name in _REQUIRED_SESSION_FIELDS):
        return None
    if values["HERMES_SESSION_PROFILE"] not in ("", "default"):
        return None
    if values["HERMES_SESSION_PLATFORM"] != "whatsapp":
        return None
    if values["HERMES_SESSION_CHAT_TYPE"] != "dm":
        return None
    return {
        "platform": values["HERMES_SESSION_PLATFORM"],
        "chat_type": values["HERMES_SESSION_CHAT_TYPE"],
        "chat_id": values["HERMES_SESSION_CHAT_ID"],
        "session_key": values["HERMES_SESSION_KEY"],
        "session_id": values["HERMES_SESSION_ID"],
    }


def _response_payload(raw: bytes) -> dict[str, str] | None:
    payload: Any = json.loads(raw)
    if not isinstance(payload, dict):
        return None
    if set(payload) == {"status", "phone"}:
        if payload["status"] == "ok" and _PHONE_RE.fullmatch(payload["phone"]):
            return {"status": "ok", "phone": payload["phone"]}
        return None
    if payload == {"status": "unavailable", "reason": "phone_not_resolved"}:
        return payload
    return None


def conversation_phone(args: dict, **kwargs: Any) -> str:
    """Return the Brain phone contract without ever raising to the model."""
    try:
        if not isinstance(args, dict) or args:
            return _unavailable()
        context = _session_context()
        if context is None:
            return _unavailable()

        token = kwargs.get("BRAIN_GATEWAY_TOKEN")
        if token is None:
            token = os.environ.get("BRAIN_GATEWAY_TOKEN", "")
        if not _safe_context_value(token):
            return _unavailable()

        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(context, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                return _unavailable()
            result = _response_payload(raw)
        return json.dumps(result, separators=(",", ":")) if result else _unavailable()
    except Exception:  # noqa: BLE001 - tool boundary must never raise
        return _unavailable()
