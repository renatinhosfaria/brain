from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.lifecycle_engine import LifecycleEngine
from brain.runtime_db import RuntimeDatabase
from brain.transport_models import RuntimeIds

ORIGIN_TURN = "waturn_" + "a" * 64
SECOND_TURN = "waturn_" + "c" * 64


class HumanInboundTests(unittest.TestCase):
    PHONE = "5534999772714"
    OTHER_PHONE = "5534999000000"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=1.0
        )
        self.runtime.initialize()
        self.ids = RuntimeIds(b"r" * 32, b"t" * 32)
        self.contact_key = self.ids.contact_key(self.PHONE)
        self.other_key = self.ids.contact_key(self.OTHER_PHONE)
        self.engine = LifecycleEngine(self.runtime, self.ids, clock=lambda: 3000.0)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_event(
        self,
        key_id: str,
        *,
        kind: str = "ordinary_inbound",
        timestamp: float = 1100.0,
        contact_key: str | None = None,
    ) -> str:
        event_id = self.ids.event_id("observer-a", key_id)
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events (event_id, observer_device_id, "
                "contact_key, direction, received_at, message_timestamp, body_hmac, "
                "body_length, native_type, transport_kind, created_at) "
                "VALUES (?, 'observer-a', ?, 'inbound', ?, ?, ?, 2, "
                "'extendedTextMessage', ?, ?)",
                (
                    event_id,
                    contact_key or self.contact_key,
                    timestamp + 0.1,
                    timestamp,
                    self.ids.body_hmac(key_id),
                    kind,
                    timestamp,
                ),
            )
        )
        return event_id

    def add_lifecycle(
        self,
        *,
        lifecycle_id: str = "fx_one",
        origin_key: str = "3EB0CTWA",
        client_id: int = 12800,
        contact_key: str | None = None,
        phase: str = "active",
        wa_turn_id: str = ORIGIN_TURN,
        origin_at: float = 1000.0,
    ) -> str:
        origin = self.add_event(
            origin_key,
            kind="ctwa_candidate",
            timestamp=origin_at,
            contact_key=contact_key,
        )
        key = contact_key or self.contact_key
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, 'g-1', 'x', ?, 'y', 2, ?, 'correlated', ?)",
                (wa_turn_id, key, origin_at, origin_at),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO lead_lifecycles (lifecycle_id, origin_event_id, "
                "wa_turn_id, contact_key, client_id, phase, last_proven_status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Sem Atendimento', ?, ?)",
                (
                    lifecycle_id,
                    origin,
                    wa_turn_id,
                    key,
                    client_id,
                    phase,
                    origin_at,
                    origin_at,
                ),
            )
        )
        return origin

    def facts(self, fact_type: str = "first_human_inbound") -> list[sqlite3.Row]:
        return self.runtime.read(
            lambda conn: conn.execute(
                "SELECT * FROM lifecycle_facts WHERE fact_type = ?", (fact_type,)
            ).fetchall()
        )

    # ------------------------------------------------------------------

    def test_ordinary_event_after_the_origin_creates_the_fact(self) -> None:
        self.add_lifecycle()
        event = self.add_event("3EB0HUMAN", timestamp=1100.0)

        self.engine.observe_transport_event(event)

        facts = self.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["lifecycle_id"], "fx_one")
        self.assertEqual(facts[0]["evidence_ref"], event)

    def test_the_fact_is_created_at_most_once(self) -> None:
        self.add_lifecycle()
        first = self.add_event("3EB0HUMAN", timestamp=1100.0)
        second = self.add_event("3EB0AGAIN", timestamp=1200.0)

        self.engine.observe_transport_event(first)
        self.engine.observe_transport_event(first)
        self.engine.observe_transport_event(second)

        facts = self.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["evidence_ref"], first)

    def test_a_second_ctwa_event_is_never_a_human_reply(self) -> None:
        """Spec 7.3: an ad-prefilled second message is attribution, not a reply."""
        self.add_lifecycle()
        event = self.add_event("3EB0SECONDAD", kind="ctwa_candidate", timestamp=1100.0)

        self.engine.observe_transport_event(event)

        self.assertEqual(self.facts(), [])

    def test_an_event_before_the_origin_creates_nothing(self) -> None:
        self.add_lifecycle(origin_at=1000.0)
        event = self.add_event("3EB0EARLIER", timestamp=900.0)

        self.engine.observe_transport_event(event)

        self.assertEqual(self.facts(), [])

    def test_an_event_without_a_lifecycle_creates_nothing(self) -> None:
        event = self.add_event("3EB0ORPHAN", timestamp=1100.0)

        self.engine.observe_transport_event(event)

        self.assertEqual(self.facts(), [])

    def test_another_contacts_event_never_reaches_this_lifecycle(self) -> None:
        self.add_lifecycle()
        event = self.add_event(
            "3EB0OTHER", timestamp=1100.0, contact_key=self.other_key
        )

        self.engine.observe_transport_event(event)

        self.assertEqual(self.facts(), [])

    def test_a_terminal_lifecycle_is_not_revived(self) -> None:
        self.add_lifecycle(phase="terminal")
        event = self.add_event("3EB0LATE", timestamp=1100.0)

        self.engine.observe_transport_event(event)

        self.assertEqual(self.facts(), [])

    def test_only_the_matching_active_lifecycle_of_a_contact_is_used(self) -> None:
        """Two lifecycles for one contact: the event belongs to the newer origin."""
        self.add_lifecycle(
            lifecycle_id="fx_old", origin_key="3EB0OLD", client_id=1, origin_at=500.0
        )
        self.add_lifecycle(
            lifecycle_id="fx_new",
            origin_key="3EB0NEW",
            client_id=2,
            wa_turn_id=SECOND_TURN,
            origin_at=1000.0,
        )
        event = self.add_event("3EB0HUMAN", timestamp=1100.0)

        self.engine.observe_transport_event(event)

        facts = self.facts()
        self.assertEqual([f["lifecycle_id"] for f in facts], ["fx_new"])

    def test_an_unknown_event_id_is_ignored(self) -> None:
        self.add_lifecycle()

        self.engine.observe_transport_event("waevt_" + "0" * 64)

        self.assertEqual(self.facts(), [])

    # ------------------------------------------------------------------

    def test_repair_scan_finds_facts_missed_while_brain_was_down(self) -> None:
        """Ingestion never blocks on lifecycle work, so a repair pass is needed."""
        self.add_lifecycle()
        self.add_event("3EB0MISSED", timestamp=1100.0)

        repaired = self.engine.repair_human_inbound_facts()

        self.assertEqual(repaired, 1)
        self.assertEqual(len(self.facts()), 1)

    def test_repair_scan_is_idempotent(self) -> None:
        self.add_lifecycle()
        self.add_event("3EB0MISSED", timestamp=1100.0)

        self.engine.repair_human_inbound_facts()
        again = self.engine.repair_human_inbound_facts()

        self.assertEqual(again, 0)
        self.assertEqual(len(self.facts()), 1)


if __name__ == "__main__":
    unittest.main()
