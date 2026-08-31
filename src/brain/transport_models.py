"""Domain-separated deterministic identifiers for Brain transport state."""

from __future__ import annotations

import hashlib
import hmac
import re

_CANONICAL_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")


class RuntimeIds:
    """Derive stable IDs from the transport secret.

    Amendment 2 removed the runtime domain along with the identifiers that
    used it: `wa_turn_id` and `effect_id` belonged to turn correlation and
    lifecycle effects, and nothing derives from that secret any more. Keeping
    an unused key in the deployment would advertise a separation that no
    longer exists.
    """

    __slots__ = ("_transport_secret",)

    def __init__(self, transport_secret: bytes) -> None:
        self._transport_secret = self._validated_secret(
            transport_secret, "transport_secret"
        )

    @staticmethod
    def _validated_secret(value: bytes, name: str) -> bytes:
        if not isinstance(value, bytes) or len(value) < 32:
            raise ValueError(f"{name} must contain at least 32 bytes")
        return bytes(value)

    @staticmethod
    def _text(value: str, name: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @classmethod
    def _digest(cls, secret: bytes, domain: str, *parts: str) -> str:
        framed = bytearray(domain.encode("ascii"))
        framed.extend(b"\0")
        for index, part in enumerate(parts):
            encoded = cls._text(part, f"part_{index}", allow_empty=True).encode("utf-8")
            framed.extend(len(encoded).to_bytes(8, "big"))
            framed.extend(encoded)
        return hmac.new(secret, bytes(framed), hashlib.sha256).hexdigest()

    def contact_key(self, canonical_phone: str) -> str:
        value = self._text(canonical_phone, "canonical_phone")
        if not _CANONICAL_PHONE_RE.fullmatch(value):
            raise ValueError("canonical_phone has invalid format")
        return self._digest(self._transport_secret, "brain.transport.contact.v1", value)

    def event_id(self, observer_device_id: str, observer_message_id: str) -> str:
        device = self._text(observer_device_id, "observer_device_id")
        message = self._text(observer_message_id, "observer_message_id")
        return "waevt_" + self._digest(
            self._transport_secret,
            "brain.transport.event.v1",
            device,
            message,
        )

    def body_hmac(self, body: str) -> str:
        return self._digest(
            self._transport_secret,
            "brain.transport.body.v1",
            self._text(body, "body", allow_empty=True),
        )

    def jid_hmac(self, jid: str) -> str:
        return self._digest(
            self._transport_secret,
            "brain.transport.jid.v1",
            self._text(jid, "jid"),
        )

    def opaque_hmac(self, value: str) -> str:
        return self._digest(
            self._transport_secret,
            "brain.transport.opaque.v1",
            self._text(value, "value", allow_empty=True),
        )
