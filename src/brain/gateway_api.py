"""Private localhost gateway bridge for the CEO session context."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .authorization import GatewaySessionContext
from .errors import BrainError
from .service import BrainService

_CONTEXT_FIELDS = frozenset(
    {"platform", "chat_type", "chat_id", "session_key", "session_id"}
)
_MAX_CONTEXT_VALUE = 512


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
            body = await request.body()
            if len(body) > 16_384:
                raise _request_error()
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
