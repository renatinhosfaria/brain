"""CEO-side tool and hooks for Brain's private gateway routes."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from typing import Any

TURN_REGISTER_ENDPOINT = "http://127.0.0.1:8765/internal/gateway/turn-register"
CONTEXT_ENDPOINT = "http://127.0.0.1:8765/internal/gateway/conversation-context"
_SESSION_FIELDS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PROFILE",
)
_REQUIRED_SESSION_FIELDS = _SESSION_FIELDS[:-1]
_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
_TECHNICAL_ID_RE = re.compile(r"^(?:waturn_|waevt_)[A-Za-z0-9_-]{1,128}$")
_TRANSPORT_KINDS = frozenset({"ctwa_candidate", "ordinary_inbound"})
_APPROVED_ASSIGNEES = frozenset({"porteiro", "cadastro", "reno"})
_MAX_RESPONSE_BYTES = 16_384
_HTTP_TIMEOUT_SECONDS = 5.0
_TURN_TTL_SECONDS = 3_600.0
_MAX_TURN_ENTRIES = 1_024
# Identifiers live only long enough to reach the turn they belong to, which
# is seconds away including Hermes' debounce. The turn map's hour-long TTL is
# wrong here: on 2026-08-30 an identifier from a message that produced no turn
# survived 57 minutes and was drained by an unrelated CTWA turn, joining to a
# length that turn's message could not have produced.
DISPATCH_TTL_SECONDS = 60.0
_MAX_DISPATCH_IDS = 64
_MAX_MESSAGE_ID = 128
_TURN_LOCK = threading.Lock()
_CURRENT_TURNS: dict[tuple[str, ...], tuple[str, str, float]] = {}
# Origin turn per chat: the wa_turn_id of the external WhatsApp turn that
# started the lead. Refreshed only on an external turn, so Cadastro and Reno
# cards created during Kanban-notification turns keep the originating turn
# (spec 10.1.1, premise P7).
_ORIGIN_TURNS: dict[tuple[str, ...], tuple[str, float]] = {}
# Raw key.id values seen by pre_gateway_dispatch, awaiting the turn that will
# carry them to Brain. Bounded, TTL-expiring, and never written to disk.
_DISPATCH_BUFFERS: dict[tuple[str, str], tuple[list[str], float]] = {}


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
    # This documented public accessor reads Hermes' active request ContextVars.
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
    value = (overrides or {}).get("BRAIN_GATEWAY_TOKEN")
    if value is None:
        value = os.environ.get("BRAIN_GATEWAY_TOKEN", "")
    return value if _safe_context_value(value) else None


def _context_key(context: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        context[field]
        for field in ("platform", "chat_type", "chat_id", "session_key", "session_id")
    )


def _forget_turn(context: dict[str, str]) -> None:
    with _TURN_LOCK:
        _CURRENT_TURNS.pop(_context_key(context), None)


def _remember_turn(context: dict[str, str], turn_id: str, wa_turn_id: str) -> None:
    now = time.monotonic()
    with _TURN_LOCK:
        expired = [
            key
            for key, value in _CURRENT_TURNS.items()
            if now - value[2] > _TURN_TTL_SECONDS
        ]
        for key in expired:
            _CURRENT_TURNS.pop(key, None)
        if len(_CURRENT_TURNS) >= _MAX_TURN_ENTRIES:
            oldest = min(_CURRENT_TURNS, key=lambda key: _CURRENT_TURNS[key][2])
            _CURRENT_TURNS.pop(oldest, None)
        _CURRENT_TURNS[_context_key(context)] = (turn_id, wa_turn_id, now)


def _current_turn(context: dict[str, str]) -> tuple[str, str] | None:
    now = time.monotonic()
    key = _context_key(context)
    with _TURN_LOCK:
        value = _CURRENT_TURNS.get(key)
        if value is None:
            return None
        if now - value[2] > _TURN_TTL_SECONDS:
            _CURRENT_TURNS.pop(key, None)
            return None
        return value[0], value[1]


def _post(payload: dict[str, Any], token: str, endpoint: str) -> object:
    request = urllib.request.Request(
        endpoint,
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


def _registration_result(payload: object) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or set(payload) != {
        "status",
        "wa_turn_id",
        "correlation",
    }:
        return None
    wa_turn_id = payload.get("wa_turn_id")
    if (
        payload.get("status") != "ok"
        or not isinstance(wa_turn_id, str)
        or not _TECHNICAL_ID_RE.fullmatch(wa_turn_id)
        or payload.get("correlation") not in {"correlated", "pending", "ambiguous"}
    ):
        return
    return wa_turn_id, str(payload["correlation"])


def _context_result(payload: object, expected_turn: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("status") == "unavailable":
        reason = payload.get("reason")
        if isinstance(reason, str) and 0 < len(reason) <= 80:
            return {"status": "unavailable", "reason": reason}
        return None
    if set(payload) != {"status", "contact", "turn", "events"}:
        return None
    contact = payload.get("contact")
    turn = payload.get("turn")
    events = payload.get("events")
    if (
        payload.get("status") != "ok"
        or not isinstance(contact, dict)
        or set(contact) != {"phone_e164", "display_name", "display_name_source"}
        or not isinstance(contact.get("phone_e164"), str)
        or not _PHONE_RE.fullmatch(contact["phone_e164"])
        or not isinstance(turn, dict)
        or turn != {"wa_turn_id": expected_turn}
        or not isinstance(events, list)
        or not events
    ):
        return None
    name = contact.get("display_name")
    source = contact.get("display_name_source")
    if name is None:
        if source is not None:
            return None
    elif (
        not isinstance(name, str)
        or not (1 <= len(name) <= 160)
        or source != "whatsapp_profile"
    ):
        return None
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event)
            != {"event_id", "transport_kind", "source_app", "inbound_kind"}
            or not isinstance(event.get("event_id"), str)
            or not _TECHNICAL_ID_RE.fullmatch(event["event_id"])
            or event.get("transport_kind") not in _TRANSPORT_KINDS
            or (
                event.get("source_app") is not None
                and not isinstance(event.get("source_app"), str)
            )
            or event.get("inbound_kind") is not None
        ):
            return None
    return payload


def _prune(
    store: dict,
    now: float,
    timestamp_index: int,
    ttl_seconds: float = _TURN_TTL_SECONDS,
) -> None:
    expired = [
        key
        for key, value in store.items()
        if now - value[timestamp_index] > ttl_seconds
    ]
    for key in expired:
        store.pop(key, None)
    if len(store) >= _MAX_TURN_ENTRIES:
        oldest = min(store, key=lambda key: store[key][timestamp_index])
        store.pop(oldest, None)


def _remember_origin(context: dict[str, str], wa_turn_id: str) -> None:
    now = time.monotonic()
    with _TURN_LOCK:
        _prune(_ORIGIN_TURNS, now, 1)
        _ORIGIN_TURNS[_context_key(context)] = (wa_turn_id, now)


def _current_origin(context: dict[str, str]) -> str | None:
    now = time.monotonic()
    with _TURN_LOCK:
        value = _ORIGIN_TURNS.get(_context_key(context))
        if value is None:
            return None
        if now - value[1] > _TURN_TTL_SECONDS:
            _ORIGIN_TURNS.pop(_context_key(context), None)
            return None
        return value[0]


def _valid_message_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_MESSAGE_ID
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def _drain_message_ids(context: dict[str, str]) -> list[str]:
    """Take the identifiers buffered for this chat since the previous turn."""
    key = (context["platform"], context["chat_id"])
    now = time.monotonic()
    with _TURN_LOCK:
        _prune(_DISPATCH_BUFFERS, now, 1, DISPATCH_TTL_SECONDS)
        buffered = _DISPATCH_BUFFERS.pop(key, None)
    return list(buffered[0]) if buffered else []


def _platform_name(value: object) -> str | None:
    """Read the platform whether it arrives as a Platform enum or a string.

    ``Platform`` is an Enum with a ``_missing_`` hook for plugin platforms, and
    not every path is guaranteed to hand this hook a member rather than the raw
    value. Accepting both removes an assumption that would otherwise fail
    silently: an unrecognised platform empties the buffer and every turn would
    become uncorrelatable.
    """
    name = getattr(value, "value", value)
    return name if isinstance(name, str) else None


def pre_gateway_dispatch(event: Any = None, **_kwargs: Any) -> None:
    """Buffer this inbound message's key.id. Performs no I/O of any kind.

    Upstream leaves this hook out of ``_HOOK_TIMEOUT_BOUNDED_HOOKS`` because
    abandoning a policy gate is unsafe either way, so it runs unbounded and to
    completion. Anything blocking here would wedge inbound dispatch for every
    message and break the spec's requirement that Hermes keeps serving while
    Brain is down. Keep this callback pure in-memory work.
    """
    try:
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False):
            return
        platform = _platform_name(getattr(source, "platform", None))
        profile = getattr(source, "profile", None) or "default"
        chat_id = getattr(source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if (
            platform != "whatsapp"
            or profile != "default"
            or getattr(source, "chat_type", None) != "dm"
            or not isinstance(chat_id, str)
            or not chat_id
            or not _valid_message_id(message_id)
        ):
            return
        now = time.monotonic()
        with _TURN_LOCK:
            _prune(_DISPATCH_BUFFERS, now, 1, DISPATCH_TTL_SECONDS)
            buffered, _ = _DISPATCH_BUFFERS.get((platform, chat_id), ([], now))
            if message_id not in buffered and len(buffered) < _MAX_DISPATCH_IDS:
                buffered.append(message_id)
            _DISPATCH_BUFFERS[(platform, chat_id)] = (buffered, now)
    except Exception:  # noqa: BLE001 - dispatch must never be blocked by us
        return
    return


def pre_llm_call(*, turn_id: str = "", user_message: str = "", **_kwargs: Any) -> None:
    """Register only the current default-profile WhatsApp DM turn."""
    context = _session_context()
    token = _token()
    if context is not None:
        _forget_turn(context)
    if (
        context is None
        or token is None
        or not _safe_context_value(turn_id)
        or not isinstance(user_message, str)
        or len(user_message) > 12_000
    ):
        return
    message_ids = _drain_message_ids(context)
    try:
        result = _registration_result(
            _post(
                {
                    **context,
                    "turn_id": turn_id,
                    "user_message": user_message,
                    "turn_timestamp": float(time.time()),
                    "message_ids": message_ids,
                },
                token,
                TURN_REGISTER_ENDPOINT,
            )
        )
        if result is not None:
            _remember_turn(context, turn_id, result[0])
            # Only an external turn may become or replace the origin.
            if message_ids:
                _remember_origin(context, result[0])
    except Exception:  # noqa: BLE001 - best-effort hook boundary
        return
    return


def pre_tool_call(
    *, tool_name: str = "", args: Any = None, turn_id: str = "", **_kwargs: Any
) -> dict[str, Any] | None:
    """Force CEO Kanban idempotency from the retained origin turn."""
    context = _session_context()
    origin = _current_origin(context) if context is not None else None
    if (
        context is None
        or origin is None
        or tool_name != "kanban_create"
        or not isinstance(args, dict)
        or args.get("assignee") not in _APPROVED_ASSIGNEES
    ):
        # With no retained origin the model-supplied key is left alone. A wrong
        # binding is silent and permanent; an unrewritten key is still
        # discoverable by the reconciler from kanban.db.
        return None
    assignee = str(args["assignee"])
    return {
        "action": "modify",
        "args": {"idempotency_key": f"whatsapp:{origin}:{assignee}"},
    }


def conversation_context(args: dict, **kwargs: Any) -> str:
    """Return only Brain's bounded context contract for the current turn."""
    try:
        context = _session_context()
        current = _current_turn(context) if context is not None else None
        token = _token(kwargs)
        if not isinstance(args, dict) or args or current is None:
            return _unavailable()
        if context is None or token is None:
            return _unavailable()
        result = _context_result(
            _post(
                {**context, "wa_turn_id": current[1]},
                token,
                CONTEXT_ENDPOINT,
            ),
            current[1],
        )
        return (
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if result is not None
            else _unavailable()
        )
    except Exception:  # noqa: BLE001 - tool boundary must never raise
        return _unavailable()
