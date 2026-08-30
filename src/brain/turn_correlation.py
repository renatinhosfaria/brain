"""Exact-identifier correlation of Hermes turns to privacy-safe transport events.

Spec section 8, as amended 2026-08-30. Hermes and the observer both see the
same WhatsApp ``key.id`` (premise P1), so a turn is joined to its transport
events by deriving each event's identifier rather than by matching content
inside a debounce window. Correlation is re-evaluated when events arrive late,
because the observer's ingestion and Hermes' turn registration are independent
pipelines with no ordering guarantee (premise P6).
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds

DEFAULT_GRACE_SECONDS = 96 * 3600

CORRELATED = "correlated"
PENDING = "pending"
UNCORRELATABLE = "uncorrelatable"


class TurnConflictError(Exception):
    """An existing opaque Hermes turn conflicts with the repeated registration."""


@dataclass(frozen=True)
class TurnRegistration:
    hermes_session_id: str
    session_key: str
    contact_key: str
    turn_id: str
    user_message: str
    turn_timestamp: float
    message_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _Resolution:
    status: str
    event_ids: tuple[str, ...] = ()


def session_key_hmac(runtime_secret: bytes, session_key: str) -> str:
    """Return a domain-separated keyed commitment to a Hermes session key."""
    if not isinstance(runtime_secret, bytes) or len(runtime_secret) < 32:
        raise ValueError("runtime_secret must contain at least 32 bytes")
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("session_key must be a non-empty string")
    encoded = session_key.encode("utf-8")
    framed = (
        b"brain.runtime.session_key.v1\0" + len(encoded).to_bytes(8, "big") + encoded
    )
    return hmac.new(runtime_secret, framed, hashlib.sha256).hexdigest()


class TurnCorrelationService:
    """Join one turn to its exact transport events, and repair late arrivals."""

    def __init__(
        self,
        runtime: RuntimeDatabase,
        runtime_ids: RuntimeIds,
        *,
        runtime_secret: bytes,
        observer_device_ids: Sequence[str] = (),
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        self.runtime = runtime
        self.runtime_ids = runtime_ids
        self.runtime_secret = bytes(runtime_secret)
        self.observer_device_ids = tuple(observer_device_ids)
        self.grace_seconds = float(grace_seconds)
        self.clock = clock

    # ------------------------------------------------------------------
    # Registration

    def register(self, registration: TurnRegistration) -> dict[str, str]:
        wa_turn_id = self.runtime_ids.wa_turn_id(registration.turn_id)

        # An internal re-invocation, such as a Kanban completion notification,
        # fires no pre_gateway_dispatch and therefore carries no identifiers
        # (premise P2). It is not an external WhatsApp turn and gets no row.
        if not registration.message_ids:
            return self._response(wa_turn_id, UNCORRELATABLE)

        body_hmac = self.runtime_ids.body_hmac(registration.user_message)
        body_length = len(registration.user_message)
        key_hmac = session_key_hmac(self.runtime_secret, registration.session_key)
        expected = (
            registration.hermes_session_id,
            key_hmac,
            registration.contact_key,
            body_hmac,
            body_length,
            registration.turn_timestamp,
        )

        def write(conn: sqlite3.Connection) -> dict[str, str]:
            existing = conn.execute(
                "SELECT hermes_session_id, session_key_hmac, contact_key, body_hmac, "
                "body_length, turn_timestamp, correlation_status FROM whatsapp_turns "
                "WHERE wa_turn_id = ?",
                (wa_turn_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing[:6]) != expected:
                    raise TurnConflictError("turn registration conflicts")
                return self._response(wa_turn_id, str(existing[6]))

            candidates = self._candidates(conn, registration.message_ids)
            resolution = self._resolve(
                conn, candidates, registration.contact_key, registration.user_message
            )
            conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (wa_turn_id, *expected, resolution.status, self.clock()),
            )
            self._persist_resolution(conn, wa_turn_id, candidates, resolution)
            return self._response(wa_turn_id, resolution.status)

        return self.runtime.write(write)

    # ------------------------------------------------------------------
    # Re-evaluation

    def reevaluate_contact(self, contact_key: str) -> int:
        """Re-resolve this contact's pending turns. Returns how many settled.

        Called after transport ingestion and from ``conversation_context()``.
        Without it a turn registered before its events were ingested would stay
        pending forever even though the matching event is already stored.
        """
        now = self.clock()

        def write(conn: sqlite3.Connection) -> int:
            pending = conn.execute(
                "SELECT wa_turn_id, body_length, created_at FROM whatsapp_turns "
                "WHERE contact_key = ? AND correlation_status = ?",
                (contact_key, PENDING),
            ).fetchall()
            settled = 0
            for turn in pending:
                wa_turn_id = str(turn["wa_turn_id"])
                candidates = self._stored_candidates(conn, wa_turn_id)
                resolution = self._resolve(
                    conn,
                    candidates,
                    contact_key,
                    None,
                    expected_body_length=int(turn["body_length"]),
                )
                if resolution.status == PENDING:
                    if now - float(turn["created_at"]) > self.grace_seconds:
                        self._settle(conn, wa_turn_id, _Resolution(UNCORRELATABLE))
                        settled += 1
                    continue
                self._settle(conn, wa_turn_id, resolution)
                settled += 1
            return settled

        return self.runtime.write(write)

    # ------------------------------------------------------------------
    # Internals

    def _candidates(
        self, conn: sqlite3.Connection, message_ids: Sequence[str]
    ) -> list[tuple[int, tuple[str, ...]]]:
        """Derive one candidate identifier per known observer device."""
        devices = set(self.observer_device_ids)
        devices.update(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT observer_device_id FROM transport_events"
            )
        )
        ordered_devices = sorted(devices)
        return [
            (
                ordinal,
                tuple(
                    self.runtime_ids.event_id(device, message_id)
                    for device in ordered_devices
                ),
            )
            for ordinal, message_id in enumerate(message_ids)
        ]

    @staticmethod
    def _stored_candidates(
        conn: sqlite3.Connection, wa_turn_id: str
    ) -> list[tuple[int, tuple[str, ...]]]:
        grouped: dict[int, list[str]] = {}
        for row in conn.execute(
            "SELECT ordinal, candidate_event_id FROM turn_candidate_events "
            "WHERE wa_turn_id = ? ORDER BY ordinal, candidate_event_id",
            (wa_turn_id,),
        ):
            grouped.setdefault(int(row["ordinal"]), []).append(
                str(row["candidate_event_id"])
            )
        return [(ordinal, tuple(grouped[ordinal])) for ordinal in sorted(grouped)]

    def _resolve(
        self,
        conn: sqlite3.Connection,
        candidates: Sequence[tuple[int, tuple[str, ...]]],
        contact_key: str,
        user_message: str | None,
        *,
        expected_body_length: int | None = None,
    ) -> _Resolution:
        if not candidates:
            return _Resolution(UNCORRELATABLE)

        resolved: list[str] = []
        lengths: list[int] = []
        digests: list[str] = []
        for _, options in candidates:
            row = self._lookup(conn, options)
            if row is None:
                return _Resolution(PENDING)
            if not hmac.compare_digest(str(row["contact_key"] or ""), contact_key):
                # Proof of impossibility, not a timing problem: this identifier
                # belongs to another contact and never will belong to this turn.
                return _Resolution(UNCORRELATABLE)
            resolved.append(str(row["event_id"]))
            lengths.append(int(row["body_length"] or 0))
            digests.append(str(row["body_hmac"] or ""))

        if not self._content_is_consistent(
            lengths, digests, user_message, expected_body_length
        ):
            return _Resolution(UNCORRELATABLE)
        return _Resolution(CORRELATED, tuple(resolved))

    @staticmethod
    def _lookup(conn: sqlite3.Connection, options: Iterable[str]) -> sqlite3.Row | None:
        for candidate in options:
            row = conn.execute(
                "SELECT event_id, contact_key, body_hmac, body_length "
                "FROM transport_events WHERE event_id = ?",
                (candidate,),
            ).fetchone()
            if row is not None:
                return row
        return None

    def _content_is_consistent(
        self,
        lengths: Sequence[int],
        digests: Sequence[str],
        user_message: str | None,
        expected_body_length: int | None,
    ) -> bool:
        """Secondary consistency check on the joined set (spec 8.3 step 7).

        Identifiers already proved which events belong to the turn, so this
        only catches a joined set that cannot have produced the turn's message.
        At registration the raw message is in memory and each event's body HMAC
        is verified against its slice. On re-evaluation the raw message is long
        gone and never persisted, so only the aggregate length is checked.
        """
        joined_length = sum(lengths) + max(len(lengths) - 1, 0)
        if user_message is None:
            return expected_body_length is None or joined_length == expected_body_length

        if joined_length != len(user_message):
            return False
        offset = 0
        for index, length in enumerate(lengths):
            slice_digest = self.runtime_ids.body_hmac(
                user_message[offset : offset + length]
            )
            if not hmac.compare_digest(slice_digest, digests[index]):
                return False
            offset += length
            if index + 1 < len(lengths):
                if user_message[offset] != "\n":
                    return False
                offset += 1
        return True

    def _persist_resolution(
        self,
        conn: sqlite3.Connection,
        wa_turn_id: str,
        candidates: Sequence[tuple[int, tuple[str, ...]]],
        resolution: _Resolution,
    ) -> None:
        if resolution.status == CORRELATED:
            conn.executemany(
                "INSERT INTO turn_events (wa_turn_id, event_id, ordinal) "
                "VALUES (?, ?, ?)",
                (
                    (wa_turn_id, event_id, ordinal)
                    for ordinal, event_id in enumerate(resolution.event_ids)
                ),
            )
            return
        if resolution.status == PENDING:
            conn.executemany(
                "INSERT INTO turn_candidate_events "
                "(wa_turn_id, ordinal, candidate_event_id) VALUES (?, ?, ?)",
                (
                    (wa_turn_id, ordinal, candidate)
                    for ordinal, options in candidates
                    for candidate in options
                ),
            )

    @staticmethod
    def _settle(
        conn: sqlite3.Connection, wa_turn_id: str, resolution: _Resolution
    ) -> None:
        conn.execute(
            "UPDATE whatsapp_turns SET correlation_status = ? WHERE wa_turn_id = ?",
            (resolution.status, wa_turn_id),
        )
        if resolution.status == CORRELATED:
            conn.executemany(
                "INSERT INTO turn_events (wa_turn_id, event_id, ordinal) "
                "VALUES (?, ?, ?)",
                (
                    (wa_turn_id, event_id, ordinal)
                    for ordinal, event_id in enumerate(resolution.event_ids)
                ),
            )
        # Terminal either way, so the awaited identifiers are no longer needed.
        conn.execute(
            "DELETE FROM turn_candidate_events WHERE wa_turn_id = ?", (wa_turn_id,)
        )

    @staticmethod
    def _response(wa_turn_id: str, status: str) -> dict[str, str]:
        return {"status": "ok", "wa_turn_id": wa_turn_id, "correlation": status}
