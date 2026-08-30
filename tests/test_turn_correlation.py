from __future__ import annotations

import hashlib
import hmac
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from brain.runtime_db import RuntimeDatabase
from brain.transport_models import RuntimeIds
from brain.turn_correlation import (
    TurnConflictError,
    TurnCorrelationService,
    TurnRegistration,
    session_key_hmac,
)


class CorrelationHarness(unittest.TestCase):
    CONTACT_PHONE = "5534999772714"
    OTHER_PHONE = "5534999000000"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self.temp_dir.name) / "runtime.db"
        self.runtime = RuntimeDatabase(self.runtime_path, timeout_seconds=1.0)
        self.runtime.initialize()
        self.runtime_secret = b"r" * 32
        self.ids = RuntimeIds(self.runtime_secret, b"t" * 32)
        self.contact_key = self.ids.contact_key(self.CONTACT_PHONE)
        self.other_contact_key = self.ids.contact_key(self.OTHER_PHONE)
        self.correlator = TurnCorrelationService(
            self.runtime,
            self.ids,
            runtime_secret=self.runtime_secret,
            window_seconds=15.0,
            clock=lambda: 2000.0,
        )
        self._event_counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_event(
        self,
        body: str,
        timestamp: float,
        *,
        contact_key: str | None = None,
        direction: str = "inbound",
        body_length: int | None = None,
        body_hmac: str | None = None,
        event_id: str | None = None,
        received_at: float | None = None,
    ) -> str:
        self._event_counter += 1
        event_id = event_id or self.ids.event_id(
            "observer-a", f"message-{self._event_counter}"
        )
        values = (
            event_id,
            "observer-a",
            contact_key or self.contact_key,
            direction,
            received_at if received_at is not None else timestamp + 0.01,
            timestamp,
            body_hmac or self.ids.body_hmac(body),
            len(body) if body_length is None else body_length,
            "conversation",
            "ordinary_inbound",
            timestamp + 0.02,
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events (event_id, observer_device_id, "
                "contact_key, direction, received_at, message_timestamp, body_hmac, "
                "body_length, native_type, transport_kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        )
        return event_id

    def registration(
        self,
        user_message: str,
        *,
        turn_id: str = "opaque-turn-1",
        timestamp: float = 1000.0,
        session_key: str = "wa:g",
        contact_key: str | None = None,
    ) -> TurnRegistration:
        return TurnRegistration(
            hermes_session_id="g-one",
            session_key=session_key,
            contact_key=contact_key or self.contact_key,
            turn_id=turn_id,
            user_message=user_message,
            turn_timestamp=timestamp,
        )

    def rows(self, table: str) -> list[sqlite3.Row]:
        return self.runtime.read(
            lambda conn: conn.execute(f"SELECT * FROM {table}").fetchall()
        )


class TurnCorrelationTests(CorrelationHarness):
    """Superseded body-HMAC correlation. Replaced during GREEN."""

    def test_single_two_three_and_embedded_newline_batches_correlate(self) -> None:
        cases = [
            (["oi"], "oi"),
            (["oi", "quero informações"], "oi\nquero informações"),
            (["um", "dois", "três"], "um\ndois\ntrês"),
            (["linha1\nlinha2", "ok"], "linha1\nlinha2\nok"),
        ]
        for index, (bodies, aggregate) in enumerate(cases):
            with self.subTest(bodies=bodies):
                self.runtime.write(lambda conn: conn.execute("DELETE FROM turn_events"))
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM whatsapp_turns")
                )
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM transport_events")
                )
                event_ids = [
                    self.add_event(body, 995.0 + offset)
                    for offset, body in enumerate(bodies)
                ]
                result = self.correlator.register(
                    self.registration(aggregate, turn_id=f"turn-{index}")
                )
                self.assertEqual(result["correlation"], "correlated")
                mappings = self.rows("turn_events")
                self.assertEqual(
                    [(row["event_id"], row["ordinal"]) for row in mappings],
                    [(event_id, ordinal) for ordinal, event_id in enumerate(event_ids)],
                )

    def test_zero_wrong_contact_out_of_window_hmac_and_length_are_pending(self) -> None:
        scenarios = (
            lambda: None,
            lambda: self.add_event("hello", 999.0, contact_key=self.other_contact_key),
            lambda: self.add_event("hello", 900.0),
            lambda: self.add_event(
                "hello", 999.0, body_hmac=self.ids.body_hmac("different")
            ),
            lambda: self.add_event("hello", 999.0, body_length=4),
        )
        for index, prepare in enumerate(scenarios):
            with self.subTest(index=index):
                self.runtime.write(lambda conn: conn.execute("DELETE FROM turn_events"))
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM whatsapp_turns")
                )
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM transport_events")
                )
                prepare()
                result = self.correlator.register(
                    self.registration("hello", turn_id=f"pending-{index}")
                )
                self.assertEqual(result["correlation"], "pending")
                self.assertEqual(self.rows("turn_events"), [])

    def test_separator_prefix_and_trailing_text_never_correlate(self) -> None:
        for index, aggregate in enumerate(("a b", "a\n", "a\nb trailing")):
            with self.subTest(aggregate=aggregate):
                self.runtime.write(lambda conn: conn.execute("DELETE FROM turn_events"))
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM whatsapp_turns")
                )
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM transport_events")
                )
                self.add_event("a", 998.0)
                self.add_event("b", 999.0)
                result = self.correlator.register(
                    self.registration(aggregate, turn_id=f"exactness-{index}")
                )
                self.assertEqual(result["correlation"], "pending")

    def test_only_exact_contiguous_ranges_are_considered(self) -> None:
        first = self.add_event("ignore", 996.0)
        wanted_a = self.add_event("a", 997.0)
        wanted_b = self.add_event("b", 998.0)
        last = self.add_event("ignore-too", 999.0)

        result = self.correlator.register(self.registration("a\nb"))

        self.assertEqual(result["correlation"], "correlated")
        mapped = [row["event_id"] for row in self.rows("turn_events")]
        self.assertEqual(mapped, [wanted_a, wanted_b])
        self.assertNotIn(first, mapped)
        self.assertNotIn(last, mapped)

    def test_two_exact_ranges_are_ambiguous_and_create_no_mappings(self) -> None:
        self.add_event("a", 996.0)
        self.add_event("b", 997.0)
        self.add_event("a", 998.0)
        self.add_event("b", 999.0)

        result = self.correlator.register(self.registration("a\nb"))

        self.assertEqual(result["correlation"], "ambiguous")
        self.assertEqual(self.rows("turn_events"), [])

    def test_identical_repeat_is_idempotent_and_conflict_fails_closed(self) -> None:
        self.add_event("hello", 999.0)
        registration = self.registration("hello")
        first = self.correlator.register(registration)
        second = self.correlator.register(registration)

        self.assertEqual(first, second)
        self.assertEqual(len(self.rows("whatsapp_turns")), 1)
        self.assertEqual(len(self.rows("turn_events")), 1)
        with self.assertRaises(TurnConflictError):
            self.correlator.register(self.registration("different"))
        self.assertEqual(len(self.rows("whatsapp_turns")), 1)
        self.assertEqual(len(self.rows("turn_events")), 1)

    def test_turn_persistence_contains_only_hmaced_identity_and_content(self) -> None:
        raw_turn = "raw-turn-value-never-store"
        raw_session = "raw-session-key-never-store"
        raw_message = "raw-user-message-never-store"
        self.add_event(raw_message, 999.0)

        result = self.correlator.register(
            self.registration(raw_message, turn_id=raw_turn, session_key=raw_session)
        )

        row = self.rows("whatsapp_turns")[0]
        self.assertEqual(result["wa_turn_id"], self.ids.wa_turn_id(raw_turn))
        self.assertEqual(row["contact_key"], self.contact_key)
        self.assertEqual(
            row["session_key_hmac"],
            session_key_hmac(self.runtime_secret, raw_session),
        )
        self.assertEqual(row["body_hmac"], self.ids.body_hmac(raw_message))
        self.assertEqual(row["body_length"], len(raw_message))
        serialized = "\n".join(
            str(tuple(row))
            for row in self.rows("whatsapp_turns") + self.rows("turn_events")
        )
        for raw in (raw_turn, raw_session, raw_message, self.CONTACT_PHONE):
            self.assertNotIn(raw, serialized)

    def test_unicode_length_is_code_points_and_matches_node_array_from(self) -> None:
        body = "A😀B"
        self.assertEqual(len(body), 3)
        self.assertEqual(len(body.encode("utf-16-le")) // 2, 4)
        self.assertEqual(len(body.encode("utf-8")), 6)
        self.add_event(body, 999.0, body_length=3)

        result = self.correlator.register(self.registration(body))

        self.assertEqual(result["correlation"], "correlated")

    def test_many_candidates_use_deterministic_temporal_order_without_count_cap(
        self,
    ) -> None:
        for index in range(400):
            self.add_event(f"distractor-{index}", 986.0 + index / 100.0)
        target = self.add_event("needle", 999.0)

        result = self.correlator.register(self.registration("needle"))

        self.assertEqual(result["correlation"], "correlated")
        self.assertEqual(
            [row["event_id"] for row in self.rows("turn_events")], [target]
        )

    def test_session_hmac_is_keyed_and_domain_separated(self) -> None:
        value = session_key_hmac(self.runtime_secret, "wa:g")
        plain_sha = hashlib.sha256(b"wa:g").hexdigest()
        generic_hmac = hmac.new(
            self.runtime_secret, b"wa:g", hashlib.sha256
        ).hexdigest()
        self.assertNotEqual(value, plain_sha)
        self.assertNotEqual(value, generic_hmac)
        self.assertEqual(value, session_key_hmac(self.runtime_secret, "wa:g"))


class ExactIdentifierCorrelationTests(CorrelationHarness):
    """Spec Amendment 1: correlate by exact message identifier, not body/window.

    Every test here targets the amended section 8 contract. They fail against
    the superseded body-HMAC implementation by design.
    """

    GRACE_SECONDS = 96 * 3600
    DEVICE = "observer-a"

    def add_event_for(self, key_id: str, body: str, timestamp: float, **kwargs) -> str:
        """Store a transport event whose event_id derives from a known key.id."""
        return self.add_event(
            body,
            timestamp,
            event_id=self.ids.event_id(self.DEVICE, key_id),
            **kwargs,
        )

    def registration_with(self, user_message: str, message_ids, **kwargs):
        return replace(
            self.registration(user_message, **kwargs),
            message_ids=tuple(message_ids),
        )

    def correlator_at(self, now: float) -> TurnCorrelationService:
        return TurnCorrelationService(
            self.runtime,
            self.ids,
            runtime_secret=self.runtime_secret,
            grace_seconds=self.GRACE_SECONDS,
            clock=lambda: now,
        )

    def test_single_identifier_correlates_without_any_time_window(self) -> None:
        self.add_event_for("3EB0A53FA395103AB5BA0C", "oi", 1000.0)
        result = self.correlator_at(1000.0).register(
            self.registration_with("oi", ["3EB0A53FA395103AB5BA0C"])
        )
        self.assertEqual(result["correlation"], "correlated")
        self.assertEqual(len(self.rows("turn_events")), 1)

    def test_event_ingested_after_registration_still_correlates(self) -> None:
        """The P6 production regression: the race must no longer be terminal.

        Observed 2026-08-30 17:38 in production. Hermes registered the turn in
        the same second the observer ingested the event; the turn stayed
        `pending` forever while the matching event sat in the database.
        """
        correlator = self.correlator_at(1000.0)
        first = correlator.register(
            self.registration_with("oi", ["3EB0A53FA395103AB5BA0C"])
        )
        self.assertEqual(first["correlation"], "pending")
        self.assertEqual(self.rows("turn_events"), [])

        self.add_event_for("3EB0A53FA395103AB5BA0C", "oi", 1000.0)
        correlator.reevaluate_contact(self.contact_key)

        stored = self.rows("whatsapp_turns")[0]
        self.assertEqual(stored["correlation_status"], "correlated")
        self.assertEqual(len(self.rows("turn_events")), 1)

    def test_batched_identifiers_keep_dispatch_order(self) -> None:
        self.add_event_for("3EB0BBB", "mundo", 1002.0)
        self.add_event_for("3EB0AAA", "oi", 1001.0)
        result = self.correlator_at(1002.0).register(
            self.registration_with("oi\nmundo", ["3EB0AAA", "3EB0BBB"])
        )
        self.assertEqual(result["correlation"], "correlated")
        ordered = [
            (row["ordinal"], row["event_id"])
            for row in sorted(self.rows("turn_events"), key=lambda r: r["ordinal"])
        ]
        self.assertEqual(
            [event_id for _, event_id in ordered],
            [
                self.ids.event_id(self.DEVICE, "3EB0AAA"),
                self.ids.event_id(self.DEVICE, "3EB0BBB"),
            ],
        )

    def test_unresolved_identifier_becomes_uncorrelatable_after_grace(self) -> None:
        correlator = self.correlator_at(1000.0)
        correlator.register(self.registration_with("oi", ["3EB0NEVERARRIVES"]))
        self.assertEqual(
            self.rows("whatsapp_turns")[0]["correlation_status"], "pending"
        )

        self.correlator_at(1000.0 + self.GRACE_SECONDS + 1).reevaluate_contact(
            self.contact_key
        )
        self.assertEqual(
            self.rows("whatsapp_turns")[0]["correlation_status"], "uncorrelatable"
        )

    def test_identifier_owned_by_another_contact_fails_closed_immediately(
        self,
    ) -> None:
        self.add_event_for(
            "3EB0OTHER", "oi", 1000.0, contact_key=self.other_contact_key
        )
        result = self.correlator_at(1000.0).register(
            self.registration_with("oi", ["3EB0OTHER"])
        )
        self.assertEqual(result["correlation"], "uncorrelatable")
        self.assertEqual(self.rows("turn_events"), [])

    def test_uncorrelatable_is_terminal_and_never_revisited(self) -> None:
        correlator = self.correlator_at(1000.0)
        correlator.register(self.registration_with("oi", ["3EB0LATE"]))
        self.correlator_at(1000.0 + self.GRACE_SECONDS + 1).reevaluate_contact(
            self.contact_key
        )

        # The event finally arrives, far too late. The verdict must not flip.
        self.add_event_for("3EB0LATE", "oi", 1000.0)
        self.correlator_at(1000.0 + self.GRACE_SECONDS + 2).reevaluate_contact(
            self.contact_key
        )
        self.assertEqual(
            self.rows("whatsapp_turns")[0]["correlation_status"], "uncorrelatable"
        )
        self.assertEqual(self.rows("turn_events"), [])

    def test_internal_reinvocation_creates_no_turn_row(self) -> None:
        """A Kanban-notification turn carries no identifiers (premise P2)."""
        result = self.correlator_at(1000.0).register(
            self.registration_with("[kanban] Task t_abc completed.", [])
        )
        self.assertEqual(result["correlation"], "uncorrelatable")
        self.assertEqual(self.rows("whatsapp_turns"), [])


if __name__ == "__main__":
    unittest.main()
