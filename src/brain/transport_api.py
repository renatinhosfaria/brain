"""Private HTTP boundary for the observer's privacy-safe event envelope."""

from __future__ import annotations

import asyncio
import json
import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse

from .errors import BrainError
from .transport_service import (
    TransportIdentityUnavailable,
    TransportRequestError,
)

_MAX_BODY_BYTES = 16_384


class TransportAPI:
    def __init__(self, service) -> None:
        self.service = service

    @staticmethod
    def _error(status_code: int, code: str) -> JSONResponse:
        return JSONResponse({"error": code}, status_code=status_code)

    async def events(self, request: Request) -> JSONResponse:
        if request.method != "POST":
            return self._error(405, "METHOD_NOT_ALLOWED")

        headers = dict(request.headers)
        try:
            # Authenticate before touching Content-Length or consuming a body.
            self.service.authorizer.parse_service_headers(headers, "transport_ingest")
        except BrainError:
            return self._error(403, "ACCESS_DENIED")

        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if not (0 <= int(declared_length) <= _MAX_BODY_BYTES):
                    return self._error(400, "TRANSPORT_REQUEST_INVALID")
            except (TypeError, ValueError):
                return self._error(400, "TRANSPORT_REQUEST_INVALID")

        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    return self._error(400, "TRANSPORT_REQUEST_INVALID")
                chunks.append(chunk)
        except (OSError, TypeError, UnicodeError):
            return self._error(400, "TRANSPORT_REQUEST_INVALID")

        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return self._error(400, "TRANSPORT_REQUEST_INVALID")

        try:
            result = await asyncio.to_thread(
                self.service.transport_service.ingest, payload
            )
        except TransportRequestError:
            return self._error(400, "TRANSPORT_REQUEST_INVALID")
        except TransportIdentityUnavailable:
            return self._error(503, "IDENTITY_UNAVAILABLE")
        except BrainError as exc:
            return self._error(
                503 if exc.unavailable else 403,
                "TRANSPORT_UNAVAILABLE" if exc.unavailable else "ACCESS_DENIED",
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            # Do not expose database or filesystem details and never log payloads.
            return self._error(503, "TRANSPORT_UNAVAILABLE")
        return JSONResponse(result, status_code=200)
