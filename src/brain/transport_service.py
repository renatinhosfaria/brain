"""Domain service for authenticated, privacy-safe transport event ingestion."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .config import BrainSettings
from .errors import DatabaseUnavailable
from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds
from .whatsapp_identity import verify_transport_identity

logger = logging.getLogger("brain.transport")

MAX_BODY_LENGTH = 10_000_000
MAX_OPAQUE_LENGTH = 10_000_000
MAX_DISPLAY_NAME_LENGTH = 160
_HEX_HMAC_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^waevt_[0-9a-f]{64}$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

_TOP_LEVEL_FIELDS = frozenset(
    {
        "event_id",
        "observer_device_id",
        "received_at",
        "message_timestamp",
        "remote_jid_hmac",
        "contact_key",
        "body_hmac",
        "body_length",
        "display_name",
        "native_type",
        "transport_kind",
        "external_ad_reply",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "event_id",
        "observer_device_id",
        "received_at",
        "remote_jid_hmac",
        "body_hmac",
        "body_length",
        "native_type",
        "transport_kind",
    }
)
_EXTERNAL_FIELDS = frozenset(
    {
        "source_type",
        "source_app",
        "source_id_present",
        "source_id_length",
        "source_id_hmac",
        "source_url_hostname",
        "source_url_length",
        "source_url_hmac",
        "ctwa_clid_present",
        "ctwa_clid_length",
        "ctwa_clid_hmac",
        "show_ad_attribution",
        "click_to_whatsapp_call",
        "contains_auto_reply",
    }
)


class TransportRequestError(Exception):
    """A safe envelope is malformed or conflicts with an existing event."""


class TransportIdentityUnavailable(Exception):
    """The observer identity cannot be proven from current evidence."""


@dataclass(frozen=True)
class TransportEnvelope:
    event_id: str
    observer_device_id: str
    received_at: float
    message_timestamp: float | None
    remote_jid_hmac: str
    contact_key: str
    body_hmac: str
    body_length: int
    display_name: str | None
    native_type: str
    transport_kind: str
    external_ad_reply: dict[str, Any] | None

    @classmethod
    def parse(cls, payload: object) -> TransportEnvelope:
        if not isinstance(payload, dict):
            raise TransportRequestError("payload must be an object")
        keys = set(payload)
        if keys - _TOP_LEVEL_FIELDS or not _REQUIRED_FIELDS.issubset(keys):
            raise TransportRequestError("safe envelope fields are invalid")

        event_id = payload["event_id"]
        if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
            raise TransportRequestError("event_id is invalid")
        observer_device_id = payload["observer_device_id"]
        if (
            not isinstance(observer_device_id, str)
            or not (1 <= len(observer_device_id) <= 128)
            or not all(0x21 <= ord(char) <= 0x7E for char in observer_device_id)
        ):
            raise TransportRequestError("observer_device_id is invalid")

        received_at = _finite_positive(payload["received_at"], "received_at")
        message_timestamp = None
        if "message_timestamp" in payload and payload["message_timestamp"] is not None:
            message_timestamp = _finite_positive(
                payload["message_timestamp"], "message_timestamp"
            )

        remote_jid_hmac = _hmac(payload["remote_jid_hmac"], "remote_jid_hmac")
        body_hmac = _hmac(payload["body_hmac"], "body_hmac")
        body_length = _bounded_integer(payload["body_length"], "body_length")
        if body_length > MAX_BODY_LENGTH:
            raise TransportRequestError("body_length is too large")
        contact_key = payload.get("contact_key")
        if contact_key is not None:
            contact_key = _hmac(contact_key, "contact_key")

        native_type = _bounded_text(payload["native_type"], "native_type", 128)
        transport_kind = payload["transport_kind"]
        if not isinstance(transport_kind, str) or transport_kind not in {
            "ctwa_candidate",
            "ordinary_inbound",
        }:
            raise TransportRequestError("transport_kind is invalid")
        display_name = _sanitize_display_name(payload.get("display_name"))
        if "external_ad_reply" in payload and payload["external_ad_reply"] is None:
            raise TransportRequestError("external_ad_reply is invalid")
        external = _parse_external(payload.get("external_ad_reply"))
        expected_kind = _expected_transport_kind(external)
        if transport_kind != expected_kind:
            raise TransportRequestError("transport_kind disagrees with metadata")

        return cls(
            event_id=event_id,
            observer_device_id=observer_device_id,
            received_at=received_at,
            message_timestamp=message_timestamp,
            remote_jid_hmac=remote_jid_hmac,
            contact_key=contact_key or "",
            body_hmac=body_hmac,
            body_length=body_length,
            display_name=display_name,
            native_type=native_type,
            transport_kind=transport_kind,
            external_ad_reply=external,
        )


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TransportRequestError(f"{name} is invalid")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise TransportRequestError(f"{name} is invalid") from None
    if not math.isfinite(number) or number <= 0:
        raise TransportRequestError(f"{name} is invalid")
    return number


def _bounded_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TransportRequestError(f"{name} is invalid")
    if value > MAX_OPAQUE_LENGTH:
        raise TransportRequestError(f"{name} is too large")
    return value


def _hmac(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HEX_HMAC_RE.fullmatch(value):
        raise TransportRequestError(f"{name} is invalid")
    return value


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= maximum)
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise TransportRequestError(f"{name} is invalid")
    return value


def _sanitize_display_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TransportRequestError("display_name is invalid")
    sanitized = "".join(
        char for char in value if not (ord(char) < 0x20 or ord(char) == 0x7F)
    )[:MAX_DISPLAY_NAME_LENGTH]
    return sanitized or None


def _optional_metadata_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, name, 128)


def _optional_hmac(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hmac(value, name)


def _parse_external(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - _EXTERNAL_FIELDS:
        raise TransportRequestError("external_ad_reply is invalid")

    source_type = _optional_metadata_text(value.get("source_type"), "source_type")
    source_app = _optional_metadata_text(value.get("source_app"), "source_app")
    source_id_present = _presence_bool(value, "source_id_present")
    source_id_length = _optional_length(
        value.get("source_id_length"), "source_id_length"
    )
    source_id_hmac = _optional_hmac(value.get("source_id_hmac"), "source_id_hmac")
    _check_presence(source_id_present, source_id_length, source_id_hmac, "source_id")

    ctwa_present = _presence_bool(value, "ctwa_clid_present")
    ctwa_length = _optional_length(value.get("ctwa_clid_length"), "ctwa_clid_length")
    ctwa_hmac = _optional_hmac(value.get("ctwa_clid_hmac"), "ctwa_clid_hmac")
    _check_presence(ctwa_present, ctwa_length, ctwa_hmac, "ctwa_clid")

    hostname = value.get("source_url_hostname")
    if hostname is not None:
        hostname = _bounded_text(hostname, "source_url_hostname", 253).lower()
        if not _HOSTNAME_RE.fullmatch(hostname) or any(
            marker in hostname for marker in (":", "/", "?", "#")
        ):
            raise TransportRequestError("source_url_hostname is invalid")
    url_length = _optional_length(value.get("source_url_length"), "source_url_length")
    url_hmac = _optional_hmac(value.get("source_url_hmac"), "source_url_hmac")
    if (hostname is None) != (url_length is None or url_hmac is None):
        raise TransportRequestError("source URL metadata is incomplete")
    if hostname is None and (url_length is not None or url_hmac is not None):
        raise TransportRequestError("source URL metadata is inconsistent")

    parsed: dict[str, Any] = {
        "source_type": source_type,
        "source_app": source_app,
        "source_id_present": source_id_present,
        "source_id_length": source_id_length,
        "source_id_hmac": source_id_hmac,
        "source_url_hostname": hostname,
        "source_url_length": url_length,
        "source_url_hmac": url_hmac,
        "ctwa_clid_present": ctwa_present,
        "ctwa_clid_length": ctwa_length,
        "ctwa_clid_hmac": ctwa_hmac,
    }
    for field in (
        "show_ad_attribution",
        "click_to_whatsapp_call",
        "contains_auto_reply",
    ):
        parsed[field] = _presence_bool(value, field)
    return parsed


def _presence_bool(values: Mapping[str, object], name: str) -> bool | None:
    if name not in values:
        return None
    value = values[name]
    if not isinstance(value, bool):
        raise TransportRequestError(f"{name} must be boolean")
    return value


def _optional_length(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _bounded_integer(value, name)


def _check_presence(
    present: bool | None, length: int | None, digest: str | None, name: str
) -> None:
    if present is True and (length is None or length <= 0 or digest is None):
        raise TransportRequestError(f"{name} metadata is incomplete")
    if present is False and (length is not None or digest is not None):
        raise TransportRequestError(f"{name} metadata is inconsistent")
    if present is None and (length is not None or digest is not None):
        raise TransportRequestError(f"{name} metadata is incomplete")


def _expected_transport_kind(external: dict[str, Any] | None) -> str:
    if (
        external
        and external.get("source_type") == "ad"
        and (
            external.get("click_to_whatsapp_call") is True
            or external.get("ctwa_clid_present") is True
            or external.get("source_id_present") is True
        )
    ):
        return "ctwa_candidate"
    return "ordinary_inbound"


class TransportService:
    """Validate, prove and persist one observer envelope transactionally."""

    def __init__(
        self,
        settings: BrainSettings,
        runtime: RuntimeDatabase,
        transport_ids: RuntimeIds | None,
        on_contact_observed: Callable[[str], object] | None = None,
    ) -> None:
        self.settings = settings
        self.runtime = runtime
        self.transport_ids = transport_ids
        # Set by BrainService to re-resolve this contact's pending turns. Kept
        # as a callback so ingestion stays independent of correlation.
        self.on_contact_observed = on_contact_observed
        # Set by BrainService to derive lifecycle facts from the new event.
        self.on_event_ingested: Callable[[str], object] | None = None

    def ingest(self, payload: object) -> dict[str, object]:
        envelope = TransportEnvelope.parse(payload)
        if self.transport_ids is None:
            raise TransportIdentityUnavailable("transport IDs are unavailable")
        identity = verify_transport_identity(
            remote_jid_hmac=envelope.remote_jid_hmac,
            contact_key=envelope.contact_key or None,
            mapping_dir=self.settings.observer_session_dir,
            transport_ids=self.transport_ids,
        )
        if identity.status != "ok" or not identity.contact_key:
            raise TransportIdentityUnavailable(identity.reason)
        envelope = replace(envelope, contact_key=identity.contact_key)
        ingestion_now = time.time()
        try:
            duplicate = self.runtime.write(
                lambda conn: self._persist(conn, envelope, ingestion_now)
            )
        except TransportRequestError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseUnavailable() from exc
        self._settle_waiting_turns(envelope.contact_key)
        self._observe_lifecycle(envelope.event_id)
        return {"status": "ok", "event_id": envelope.event_id, "duplicate": duplicate}

    def _observe_lifecycle(self, event_id: str) -> None:
        """Derive lifecycle facts, best-effort and after the durable write.

        Same contract as turn re-evaluation: the observer's acknowledgement
        must never depend on lifecycle work succeeding. Reconciliation repairs
        whatever this misses.
        """
        if self.on_event_ingested is None:
            return
        try:
            self.on_event_ingested(event_id)
        except Exception:  # noqa: BLE001 - ingestion durability wins
            logger.warning("lifecycle observation failed after transport ingestion")

    def _settle_waiting_turns(self, contact_key: str) -> None:
        """Re-resolve pending turns now that one more event exists.

        Deliberately best-effort and after the durable write: the observer's
        acknowledgement must never depend on correlation succeeding, and
        reconciliation repairs whatever this misses.
        """
        if self.on_contact_observed is None or not contact_key:
            return
        try:
            self.on_contact_observed(contact_key)
        except Exception:  # noqa: BLE001 - ingestion durability wins
            logger.warning("turn re-evaluation failed after transport ingestion")

    def _persist(
        self,
        conn: sqlite3.Connection,
        envelope: TransportEnvelope,
        ingestion_now: float,
    ) -> bool:
        values = self._event_values(envelope, ingestion_now)
        row = conn.execute(
            "SELECT observer_device_id, contact_key, direction, received_at, "
            "message_timestamp, body_hmac, body_length, native_type, transport_kind, "
            "source_type, source_app, source_id_present, source_id_length, source_id_hmac, "
            "source_url_hostname, source_url_length, source_url_hmac, ctwa_clid_present, "
            "ctwa_clid_length, ctwa_clid_hmac, show_ad_attribution, "
            "click_to_whatsapp_call, contains_auto_reply FROM transport_events "
            "WHERE event_id = ?",
            (envelope.event_id,),
        ).fetchone()
        if row is not None:
            existing = tuple(row)
            if existing != values[1:-1]:
                raise TransportRequestError("event_id conflicts with existing event")
            self._persist_ephemera(conn, envelope, ingestion_now)
            return True

        conn.execute(
            "INSERT INTO transport_events (event_id, observer_device_id, contact_key, "
            "direction, received_at, message_timestamp, body_hmac, body_length, "
            "native_type, transport_kind, source_type, source_app, source_id_present, "
            "source_id_length, source_id_hmac, source_url_hostname, source_url_length, "
            "source_url_hmac, ctwa_clid_present, ctwa_clid_length, ctwa_clid_hmac, "
            "show_ad_attribution, click_to_whatsapp_call, contains_auto_reply, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        self._persist_ephemera(conn, envelope, ingestion_now)
        return False

    def _persist_ephemera(
        self,
        conn: sqlite3.Connection,
        envelope: TransportEnvelope,
        ingestion_now: float,
    ) -> None:
        if not envelope.display_name:
            return
        if self.transport_ids is None:
            raise DatabaseUnavailable()
        display_hmac = self.transport_ids.opaque_hmac(envelope.display_name)
        expires_at = ingestion_now + self.settings.display_name_ttl_hours * 3600
        conn.execute(
            "INSERT INTO contact_ephemera (contact_key, display_name, display_name_hmac, "
            "expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(contact_key) DO UPDATE SET display_name=excluded.display_name, "
            "display_name_hmac=excluded.display_name_hmac, expires_at=excluded.expires_at, "
            "updated_at=excluded.updated_at",
            (
                envelope.contact_key,
                envelope.display_name,
                display_hmac,
                expires_at,
                ingestion_now,
                ingestion_now,
            ),
        )

    @staticmethod
    def _event_values(
        envelope: TransportEnvelope, ingestion_now: float
    ) -> tuple[object, ...]:
        external = envelope.external_ad_reply or {}
        metadata = (
            external.get("source_type"),
            external.get("source_app"),
            TransportService._database_bool(external.get("source_id_present")),
            external.get("source_id_length"),
            external.get("source_id_hmac"),
            external.get("source_url_hostname"),
            external.get("source_url_length"),
            external.get("source_url_hmac"),
            TransportService._database_bool(external.get("ctwa_clid_present")),
            external.get("ctwa_clid_length"),
            external.get("ctwa_clid_hmac"),
            TransportService._database_bool(external.get("show_ad_attribution")),
            TransportService._database_bool(external.get("click_to_whatsapp_call")),
            TransportService._database_bool(external.get("contains_auto_reply")),
        )
        return (
            envelope.event_id,
            envelope.observer_device_id,
            envelope.contact_key,
            "inbound",
            envelope.received_at,
            envelope.message_timestamp,
            envelope.body_hmac,
            envelope.body_length,
            envelope.native_type,
            envelope.transport_kind,
            *metadata,
            ingestion_now,
        )

    def _database_bool(value: bool | None) -> int | None:
        return None if value is None else int(value)
