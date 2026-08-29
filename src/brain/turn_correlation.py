"""Fail-closed correlation of Hermes turns to privacy-safe transport events."""

from __future__ import annotations

import bisect
import hashlib
import hmac
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds

DEFAULT_TURN_WINDOW_SECONDS = 15.0


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
    """Register one turn and prove one unique contiguous transport range."""

    def __init__(
        self,
        runtime: RuntimeDatabase,
        runtime_ids: RuntimeIds,
        *,
        runtime_secret: bytes,
        window_seconds: float = DEFAULT_TURN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.runtime = runtime
        self.runtime_ids = runtime_ids
        self.runtime_secret = bytes(runtime_secret)
        self.window_seconds = float(window_seconds)
        self.clock = clock

    def register(self, registration: TurnRegistration) -> dict[str, str]:
        wa_turn_id = self.runtime_ids.wa_turn_id(registration.turn_id)
        body_hmac = self.runtime_ids.body_hmac(registration.user_message)
        body_length = len(registration.user_message)
        key_hmac = session_key_hmac(self.runtime_secret, registration.session_key)

        def write(conn: sqlite3.Connection) -> dict[str, str]:
            existing = conn.execute(
                "SELECT hermes_session_id, session_key_hmac, contact_key, body_hmac, "
                "body_length, turn_timestamp, correlation_status FROM whatsapp_turns "
                "WHERE wa_turn_id = ?",
                (wa_turn_id,),
            ).fetchone()
            expected = (
                registration.hermes_session_id,
                key_hmac,
                registration.contact_key,
                body_hmac,
                body_length,
                registration.turn_timestamp,
            )
            if existing is not None:
                if tuple(existing[:6]) != expected:
                    raise TurnConflictError("turn registration conflicts")
                return self._response(wa_turn_id, str(existing[6]))

            candidates = conn.execute(
                "SELECT event_id, message_timestamp, received_at, body_hmac, "
                "body_length FROM transport_events WHERE direction = 'inbound' "
                "AND contact_key = ? AND body_hmac IS NOT NULL "
                "AND body_length IS NOT NULL "
                "AND COALESCE(message_timestamp, received_at) BETWEEN ? AND ? "
                "ORDER BY message_timestamp IS NULL, message_timestamp, "
                "received_at, event_id",
                (
                    registration.contact_key,
                    registration.turn_timestamp - self.window_seconds,
                    registration.turn_timestamp + self.window_seconds,
                ),
            ).fetchall()
            matches = self._matching_ranges(registration.user_message, candidates)
            if not matches:
                status = "pending"
            elif len(matches) == 1:
                status = "correlated"
            else:
                status = "ambiguous"

            conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wa_turn_id,
                    registration.hermes_session_id,
                    key_hmac,
                    registration.contact_key,
                    body_hmac,
                    body_length,
                    registration.turn_timestamp,
                    status,
                    self.clock(),
                ),
            )
            if status == "correlated":
                conn.executemany(
                    "INSERT INTO turn_events (wa_turn_id, event_id, ordinal) "
                    "VALUES (?, ?, ?)",
                    (
                        (wa_turn_id, event_id, ordinal)
                        for ordinal, event_id in enumerate(matches[0])
                    ),
                )
            return self._response(wa_turn_id, status)

        return self.runtime.write(write)

    @staticmethod
    def _response(wa_turn_id: str, status: str) -> dict[str, str]:
        return {"status": "ok", "wa_turn_id": wa_turn_id, "correlation": status}

    def _matching_ranges(
        self, user_message: str, candidates: list[sqlite3.Row]
    ) -> list[list[str]]:
        """Find exact ranges without enumerating permutations or subsets.

        ``weighted_prefix`` includes one virtual separator per event. This
        makes it strictly increasing even for empty bodies and permits one
        binary-search candidate end per start position.
        """
        weighted_prefix = [0]
        for row in candidates:
            weighted_prefix.append(weighted_prefix[-1] + int(row["body_length"]) + 1)

        target_length = len(user_message)
        matches: list[list[str]] = []
        digest_cache: dict[tuple[int, int], str] = {}
        for start in range(len(candidates)):
            wanted = weighted_prefix[start] + target_length + 1
            stop = bisect.bisect_left(weighted_prefix, wanted, start + 1)
            if stop >= len(weighted_prefix) or weighted_prefix[stop] != wanted:
                continue
            rows = candidates[start:stop]
            if self._range_matches(user_message, rows, digest_cache):
                matches.append([str(row["event_id"]) for row in rows])
                if len(matches) > 1:
                    return matches
        return matches

    def _range_matches(
        self,
        user_message: str,
        rows: list[sqlite3.Row],
        digest_cache: dict[tuple[int, int], str],
    ) -> bool:
        offset = 0
        for index, row in enumerate(rows):
            length = int(row["body_length"])
            end = offset + length
            if end > len(user_message):
                return False
            cache_key = (offset, end)
            digest = digest_cache.get(cache_key)
            if digest is None:
                digest = self.runtime_ids.body_hmac(user_message[offset:end])
                digest_cache[cache_key] = digest
            if not hmac.compare_digest(digest, str(row["body_hmac"])):
                return False
            offset = end
            if index + 1 < len(rows):
                if offset >= len(user_message) or user_message[offset] != "\n":
                    return False
                offset += 1
        return offset == len(user_message)
