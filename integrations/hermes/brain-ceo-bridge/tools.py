"""Trusted WhatsApp transport context for the CEO, read from Brain.

Amendment 2 removed the turn-correlation spine, and this plugin shrank with
it. What is gone matters as much as what remains: there are no hooks here any
more. `pre_gateway_dispatch` buffered raw message identifiers,
`pre_llm_call` registered every turn against Brain, and `pre_tool_call`
rewrote Kanban idempotency keys from the retained origin turn. All three
existed to make an automated CRM write exact.

Removing `pre_llm_call` in particular removes a failure this design proved in
production on 2026-08-31: it performed a synchronous network call inside the
turn path, and cleared the retained turn before re-registering it. One Brain
response of 10.153 ms against a 5 s timeout left the CEO with no context at
all, and the hook's fail-open contract meant nothing anywhere recorded it.

What is left is one tool that answers one question, on demand, with no
ambient state: who is on the other side of this WhatsApp DM, and did they
arrive from an ad. Failure is always a bounded `unavailable` payload; this
module must never raise into Hermes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

CONTEXT_ENDPOINT = "http://127.0.0.1:8765/internal/gateway/conversation-context"

_SESSION_FIELDS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PROFILE",
)
# The profile is allowed to be empty (the implicit default CEO); every other
# field must be present for the request to mean anything.
_REQUIRED_SESSION_FIELDS = _SESSION_FIELDS[:-1]

_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
_EVENT_ID_RE = re.compile(r"^waevt_[A-Za-z0-9_-]{1,128}$")
_TRANSPORT_KINDS = frozenset({"ctwa_candidate", "ordinary_inbound"})
_MAX_RESPONSE_BYTES = 16_384
_MAX_EVENTS = 32
_HTTP_TIMEOUT_SECONDS = 5.0


def _unavailable(reason: str = "context_unavailable") -> str:
    return json.dumps(
        {"status": "unavailable", "reason": reason}, separators=(",", ":")
    )


def _safe_context_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and all(0x21 <= ord(char) <= 0x7E for char in value)
        and "${" not in value
    )


def _session_context() -> dict[str, str] | None:
    """Read Hermes' active request ContextVars through its public accessor."""
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


def _token(overrides: dict[str, Any] | None = None) -> str | None:
    candidate = (overrides or {}).get("token") or os.environ.get(
        "BRAIN_GATEWAY_TOKEN", ""
    )
    return candidate if isinstance(candidate, str) and candidate else None


def _post(payload: dict[str, Any], token: str) -> object:
    request = urllib.request.Request(
        CONTEXT_ENDPOINT,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("Brain response exceeded limit")
    return json.loads(raw)


def _valid_event(event: object) -> bool:
    return (
        isinstance(event, dict)
        and set(event) == {"event_id", "transport_kind", "source_app", "inbound_kind"}
        and isinstance(event.get("event_id"), str)
        and _EVENT_ID_RE.fullmatch(event["event_id"]) is not None
        and event.get("transport_kind") in _TRANSPORT_KINDS
        and (
            event.get("source_app") is None
            or isinstance(event.get("source_app"), str)
        )
        # Transport evidence only. Lifecycle-relative meaning was never this
        # plugin's to assert, and since Amendment 2 nothing derives it at all.
        and event.get("inbound_kind") is None
    )


def _valid_contact(contact: object) -> bool:
    if (
        not isinstance(contact, dict)
        or set(contact) != {"phone_e164", "display_name", "display_name_source"}
        or not isinstance(contact.get("phone_e164"), str)
        or _PHONE_RE.fullmatch(contact["phone_e164"]) is None
    ):
        return False
    name = contact.get("display_name")
    source = contact.get("display_name_source")
    if name is None:
        return source is None
    return (
        isinstance(name, str)
        and 1 <= len(name) <= 160
        and source == "whatsapp_profile"
    )


def _context_result(payload: object) -> dict[str, Any] | None:
    """Accept only Brain's exact contract, fail closed on anything else."""
    if not isinstance(payload, dict):
        return None
    if payload.get("status") == "unavailable":
        reason = payload.get("reason")
        if isinstance(reason, str) and 0 < len(reason) <= 80:
            return {"status": "unavailable", "reason": reason}
        return None
    events = payload.get("events")
    if (
        payload.get("status") != "ok"
        or set(payload) != {"status", "contact", "events"}
        or not _valid_contact(payload.get("contact"))
        or not isinstance(events, list)
        or not (0 < len(events) <= _MAX_EVENTS)
        or not all(_valid_event(event) for event in events)
    ):
        return None
    return payload


def conversation_context(args: dict, **kwargs: Any) -> str:
    """Return Brain's bounded context for the contact in this WhatsApp DM."""
    try:
        if not isinstance(args, dict) or args:
            return _unavailable()
        context = _session_context()
        token = _token(kwargs)
        if context is None or token is None:
            return _unavailable()
        result = _context_result(_post(context, token))
        if result is None:
            return _unavailable()
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001 - a tool boundary must never raise
        return _unavailable()
