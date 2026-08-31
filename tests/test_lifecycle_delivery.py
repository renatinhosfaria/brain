from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.hermes_evidence import DeliveredObligation, TerminalRun
from brain.lifecycle_engine import LifecycleEngine
from brain.runtime_db import RuntimeDatabase
from brain.transport_models import RuntimeIds
from brain.turn_correlation import session_key_hmac

ORIGIN_TURN = "waturn_" + "a" * 64
RUNTIME_SECRET = b"r" * 32
SESSION_KEY = "agent:main:whatsapp:dm:553499772714"
T1_TEXT = "Olá! Vi que você se interessou pelo Union Vereda. Posso ajudar?"


class DeliveryProofTests(unittest.TestCase):
    PHONE = "5534999772714"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=1.0
        )
        self.runtime.initialize()
        self.ids = RuntimeIds(RUNTIME_SECRET, b"t" * 32)
        self.contact_key = self.ids.contact_key(self.PHONE)
        self.engine = LifecycleEngine(
            self.runtime,
            self.ids,
            runtime_secret=RUNTIME_SECRET,
            clock=lambda: 3000.0,
        )
        self.lifecycle_id = self.seed_lifecycle()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def seed_lifecycle(self) -> str:
        origin = self.ids.event_id("observer-a", "3EB0CTWA")
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events (event_id, observer_device_id, "
                "contact_key, direction, received_at, message_timestamp, body_hmac, "
                "body_length, native_type, transport_kind, created_at) "
                "VALUES (?, 'observer-a', ?, 'inbound', 1000.1, 1000.0, 'h', 2, "
                "'extendedTextMessage', 'ctwa_candidate', 1000.0)",
                (origin, self.contact_key),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, 'g-1', ?, ?, 'y', 2, 1000.0, 'correlated', 1000.0)",
                (
                    ORIGIN_TURN,
                    session_key_hmac(RUNTIME_SECRET, SESSION_KEY),
                    self.contact_key,
                ),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO lead_lifecycles (lifecycle_id, origin_event_id, "
                "wa_turn_id, contact_key, client_id, phase, last_proven_status, "
                "created_at, updated_at) "
                "VALUES ('fx_one', ?, ?, ?, 12800, 'active', 'Sem Atendimento', "
                "1000.0, 1000.0)",
                (origin, ORIGIN_TURN, self.contact_key),
            )
        )
        return "fx_one"

    @staticmethod
    def reno_run(
        *, response_ready: object = T1_TEXT, ended_at: float | None = 1100.0
    ) -> TerminalRun:
        return TerminalRun(
            run_id=104,
            task_id="t_reno",
            status="done",
            summary="Primeira resposta enviada",
            started_at=1050.0,
            ended_at=ended_at,
            decision="RESPOSTA_PRONTA",
            entities={},
            response_ready=response_ready if response_ready is not None else None,
        )

    @staticmethod
    def obligation(
        obligation_id: str = "o_1",
        *,
        content: str = T1_TEXT,
        session_key: str = SESSION_KEY,
        updated_at: float = 1120.0,
    ) -> DeliveredObligation:
        return DeliveredObligation(
            obligation_id=obligation_id,
            session_key=session_key,
            content=content,
            state="delivered",
            created_at=updated_at - 5,
            updated_at=updated_at,
        )

    def facts(self) -> list[sqlite3.Row]:
        return self.runtime.read(
            lambda conn: conn.execute(
                "SELECT * FROM lifecycle_facts WHERE fact_type = ?",
                ("first_t1_send_success",),
            ).fetchall()
        )

    def prove(self, run=None, obligations=None):
        return self.engine.prove_first_t1_send(
            self.lifecycle_id,
            run or self.reno_run(),
            list(self.obligation() for _ in range(1))
            if obligations is None
            else obligations,
        )

    # ------------------------------------------------------------------

    def test_exactly_one_matching_delivery_proves_the_send(self) -> None:
        match = self.prove()

        self.assertEqual(match.status, "proven")
        self.assertEqual(match.obligation_id, "o_1")
        facts = self.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["evidence_ref"], "o_1")
        self.assertEqual(facts[0]["observed_at"], 1120.0)

    def test_no_matching_delivery_is_not_proven(self) -> None:
        match = self.prove(obligations=[])

        self.assertEqual(match.status, "not_proven")
        self.assertEqual(self.facts(), [])

    def test_different_content_does_not_prove_the_send(self) -> None:
        match = self.prove(
            obligations=[self.obligation(content="Outra mensagem qualquer")]
        )

        self.assertEqual(match.status, "not_proven")
        self.assertEqual(self.facts(), [])

    def test_another_session_does_not_prove_the_send(self) -> None:
        match = self.prove(
            obligations=[self.obligation(session_key="agent:main:whatsapp:dm:5511")]
        )

        self.assertEqual(match.status, "not_proven")
        self.assertEqual(self.facts(), [])

    def test_delivery_before_the_run_does_not_prove_the_send(self) -> None:
        """An earlier outbound cannot be the reply this run produced."""
        match = self.prove(obligations=[self.obligation(updated_at=1000.0)])

        self.assertEqual(match.status, "not_proven")
        self.assertEqual(self.facts(), [])

    def test_two_indistinguishable_deliveries_are_ambiguous(self) -> None:
        """Spec 15: zero or multiple matches are NOT_PROVEN, never a guess."""
        match = self.prove(
            obligations=[
                self.obligation("o_1"),
                self.obligation("o_2", updated_at=1130.0),
            ]
        )

        self.assertEqual(match.status, "ambiguous")
        self.assertEqual(self.facts(), [])

    def test_a_run_without_response_ready_proves_nothing(self) -> None:
        for empty in (None, "", "   "):
            with self.subTest(response_ready=empty):
                match = self.prove(run=self.reno_run(response_ready=empty))
                self.assertEqual(match.status, "not_proven")
                self.assertEqual(self.facts(), [])

    def test_the_fact_is_recorded_once(self) -> None:
        self.prove()
        second = self.prove()

        self.assertEqual(second.status, "proven")
        self.assertEqual(len(self.facts()), 1)

    def test_no_raw_message_text_is_persisted(self) -> None:
        self.prove()

        stored = json.dumps(
            [dict(row) for row in self.facts()], default=str
        ) + json.dumps(
            [
                dict(row)
                for row in self.runtime.read(
                    lambda conn: conn.execute(
                        "SELECT * FROM lead_lifecycles"
                    ).fetchall()
                )
            ],
            default=str,
        )
        self.assertNotIn(T1_TEXT, stored)
        self.assertNotIn(SESSION_KEY, stored)

    def test_a_lifecycle_without_a_session_commitment_proves_nothing(self) -> None:
        self.runtime.write(
            lambda conn: conn.execute(
                "UPDATE whatsapp_turns SET session_key_hmac = NULL"
            )
        )

        match = self.prove()

        self.assertEqual(match.status, "not_proven")
        self.assertEqual(self.facts(), [])

    # ------------------------------------------------------------------

    def test_proof_at_risk_flags_old_lifecycles_without_the_fact(self) -> None:
        """Hermes keeps obligations about seven days; missing proof expires."""
        at_risk = self.engine.lifecycles_with_delivery_proof_at_risk(
            now=1000.0 + 6 * 86400, retention_seconds=7 * 86400
        )
        self.assertEqual(at_risk, [self.lifecycle_id])

    def test_proof_at_risk_ignores_lifecycles_that_already_have_the_fact(self) -> None:
        self.prove()

        at_risk = self.engine.lifecycles_with_delivery_proof_at_risk(
            now=1000.0 + 6 * 86400, retention_seconds=7 * 86400
        )
        self.assertEqual(at_risk, [])

    def test_proof_at_risk_ignores_recent_lifecycles(self) -> None:
        at_risk = self.engine.lifecycles_with_delivery_proof_at_risk(
            now=1000.0 + 3600, retention_seconds=7 * 86400
        )
        self.assertEqual(at_risk, [])


if __name__ == "__main__":
    unittest.main()
