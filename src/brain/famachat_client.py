"""Deterministic FamaChat access for the lifecycle writer. No model, no LLM.

Spec section 16. The writer proves the record in front of it is the right one
before touching anything, so every read here is strict: a body that is not the
exact shape the captured schema describes is unavailable, never a partial
reading. A half-parsed record is what leads to changing the wrong lead.

Transport is injected. The client itself knows the MCP envelope
``{status, statusText, body, truncated}`` and nothing about how it arrived.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

GET_CLIENT_TOOL = "fc_get_clientes_by_id"

_DIGITS = re.compile(r"\D+")
_COUNTRY = "55"


class FamaChatUnavailable(Exception):
    """FamaChat could not answer in a shape the writer is allowed to act on."""


@dataclass(frozen=True)
class FamaChatClientRecord:
    client_id: int
    phone: str
    broker_id: int | None
    status: str
    source: str | None


def _national(phone: object) -> str | None:
    """Reduce a phone to its national digits, or nothing if it is not one."""
    if not isinstance(phone, str):
        return None
    digits = _DIGITS.sub("", phone)
    if digits.startswith(_COUNTRY) and len(digits) > 10:
        digits = digits[len(_COUNTRY) :]
    return digits if 10 <= len(digits) <= 11 else None


def same_phone(left: object, right: object) -> bool:
    """Compare two Brazilian numbers across the mobile ninth-digit change.

    Brain resolves the current mobile form from WhatsApp evidence, which
    carries the ninth digit. FamaChat may hold the older ten-digit form for the
    same person. Treating those as different people would refuse every effect;
    treating unrelated numbers as equal would change the wrong lead, so the
    only tolerated difference is that one digit in that one position.

    Neither value is ever logged.
    """
    first, second = _national(left), _national(right)
    if first is None or second is None:
        return False
    if first == second:
        return True

    long_form, short_form = (
        (first, second) if len(first) > len(second) else (second, first)
    )
    if len(long_form) != 11 or len(short_form) != 10:
        return False
    # The ninth digit was prepended to mobiles only, and only right after the
    # area code. A landline subscriber number starts 2-5 and never gained one,
    # so stretching one into a mobile would equate two different people.
    if long_form[2] != "9" or short_form[2] not in "6789":
        return False
    return long_form[:2] + long_form[3:] == short_form


class FamaChatClient:
    def __init__(self, transport: Callable[[str, dict], dict]) -> None:
        self.transport = transport

    def get_client(self, client_id: int) -> FamaChatClientRecord | None:
        """Read one client by exact id. ``None`` means it does not exist."""
        response = self._call(GET_CLIENT_TOOL, {"id": client_id})
        status = response.get("status")

        if status == 404:
            return None
        if status != 200:
            raise FamaChatUnavailable(f"unexpected status {status}")
        if response.get("truncated"):
            # A trimmed record could omit the very field being validated.
            raise FamaChatUnavailable("response was truncated")

        body = response.get("body")
        if not isinstance(body, dict):
            raise FamaChatUnavailable("response body is not an object")

        record_id = body.get("id")
        record_status = body.get("status")
        broker_id = body.get("brokerId")
        if not isinstance(record_id, int) or isinstance(record_id, bool):
            raise FamaChatUnavailable("client id is missing or malformed")
        if not isinstance(record_status, str) or not record_status:
            raise FamaChatUnavailable("client status is missing")
        if not isinstance(broker_id, int) or isinstance(broker_id, bool):
            raise FamaChatUnavailable("client broker is missing or malformed")

        phone = body.get("phone")
        source = body.get("source")
        return FamaChatClientRecord(
            client_id=record_id,
            phone=phone if isinstance(phone, str) else "",
            broker_id=broker_id,
            status=record_status,
            source=source if isinstance(source, str) else None,
        )

    def _call(self, tool: str, arguments: dict) -> dict:
        try:
            response = self.transport(tool, arguments)
        except FamaChatUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary
            # Never surface the underlying message: it can carry the endpoint,
            # the credential, or the record itself.
            raise FamaChatUnavailable(
                f"transport failed: {type(exc).__name__}"
            ) from None
        if not isinstance(response, dict):
            raise FamaChatUnavailable("response envelope is not an object")
        return response
