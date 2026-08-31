"""Private localhost gateway bridge for the CEO session context."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .authorization import GatewaySessionContext
from .errors import BrainError
from .service import BrainService

_CONTEXT_FIELDS = frozenset(
    {"platform", "chat_type", "chat_id", "session_key", "session_id"}
)
_TURN_FIELDS = _CONTEXT_FIELDS | frozenset(
    {"turn_id", "user_message", "turn_timestamp", "message_ids"}
)
_REQUIRED_TURN_FIELDS = _TURN_FIELDS - frozenset({"message_ids"})
# Amendment 2: the context request carries only the session identity. The
# turn it belonged to is no longer part of the contract.
_CONVERSATION_CONTEXT_FIELDS = _CONTEXT_FIELDS
_MAX_CONTEXT_VALUE = 512
_MAX_BODY_BYTES = 16_384


def _valid_context_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_CONTEXT_VALUE
        and all(0x21 <= ord(char) <= 0x7E for char in value)
        and "${" not in value
    )


def _request_error() -> BrainError:
    return BrainError("GATEWAY_REQUEST_INVALID")


def _parse_context(payload: object) -> GatewaySessionContext:
    if not isinstance(payload, dict) or set(payload) != _CONTEXT_FIELDS:
        raise _request_error()
    if any(not _valid_context_value(value) for value in payload.values()):
        raise _request_error()
    if payload["platform"] != "whatsapp" or payload["chat_type"] != "dm":
        raise _request_error()
    return GatewaySessionContext(
        platform=payload["platform"],
        chat_type=payload["chat_type"],
        chat_id=payload["chat_id"],
        session_key=payload["session_key"],
        session_id=payload["session_id"],
    )


@dataclass(frozen=True)
class ConversationContextPayload:
    context: GatewaySessionContext


def _parse_conversation_context(payload: object) -> ConversationContextPayload:
    if not isinstance(payload, dict) or set(payload) != _CONVERSATION_CONTEXT_FIELDS:
        raise _request_error()
    context = _parse_context({key: payload[key] for key in _CONTEXT_FIELDS})
    return ConversationContextPayload(context)


class GatewayAPI:
    def __init__(self, service: BrainService) -> None:
        self.service = service

    @staticmethod
    def _response_for_error(error: BrainError) -> JSONResponse:
        if error.unavailable:
            status_code = 503
        elif error.code == "GATEWAY_REQUEST_INVALID":
            status_code = 400
        else:
            status_code = 403
        return JSONResponse({"error": error.public_message}, status_code=status_code)

    async def conversation_phone(self, request: Request) -> JSONResponse:
        headers = dict(request.headers)
        try:
            # Authenticate before reading or parsing the body, especially so an
            # unresolved secret cannot reach the JSON or database paths.
            self.service.authorizer.parse_gateway_headers(headers)
            declared_length = request.headers.get("content-length")
            if declared_length is not None:
                try:
                    if not (0 <= int(declared_length) <= _MAX_BODY_BYTES):
                        raise _request_error()
                except ValueError:
                    raise _request_error() from None
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    raise _request_error()
                chunks.append(chunk)
            body = b"".join(chunks)
            try:
                payload: Any = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                raise _request_error() from None
            context = _parse_context(payload)
            result = await asyncio.to_thread(
                self.service.gateway_conversation_phone, headers, context
            )
            return JSONResponse(result, status_code=200)
        except BrainError as exc:
            return self._response_for_error(exc)
        except (OSError, TypeError, UnicodeError, ValueError):
            return self._response_for_error(_request_error())

    async def conversation_context(self, request: Request) -> JSONResponse:
        if request.method != "POST":
            return JSONResponse({"error": "method not allowed"}, status_code=405)
        headers = dict(request.headers)
        try:
            self.service.authorizer.parse_gateway_headers(
                headers, "conversation_context"
            )
            declared_length = request.headers.get("content-length")
            if declared_length is not None:
                try:
                    if not (0 <= int(declared_length) <= _MAX_BODY_BYTES):
                        raise _request_error()
                except ValueError:
                    raise _request_error() from None
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    raise _request_error()
                chunks.append(chunk)
            try:
                payload: Any = json.loads(b"".join(chunks))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                raise _request_error() from None
            parsed = _parse_conversation_context(payload)
            result = await asyncio.to_thread(
                self.service.gateway_conversation_context,
                headers,
                parsed.context,
            )
            return JSONResponse(result, status_code=200)
        except BrainError as exc:
            return self._response_for_error(exc)
        except (OSError, TypeError, UnicodeError, ValueError):
            return self._response_for_error(_request_error())
