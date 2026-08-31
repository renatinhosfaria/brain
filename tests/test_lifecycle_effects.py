from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.lifecycle_engine import LifecycleEngine
from brain.lifecycle_models import (
    ALLOWED_TRANSITIONS,
    EM_ATENDIMENTO,
    FACT_FIRST_HUMAN_INBOUND,
    FACT_FIRST_T1_SEND_SUCCESS,
    NAO_RESPONDEU,
    SEM_ATENDIMENTO,
    desired_status,
)
from brain.runtime_db import RuntimeDatabase
from brain.transport_models import RuntimeIds

ORIGIN_TURN = "waturn_" + "a" * 64


class DesiredStatusTests(unittest.TestCase):
    def test_the_state_table_is_a_pure_function_of_facts(self) -> None:
        cases = (
            (set(), SEM_ATENDIMENTO),
            ({FACT_FIRST_T1_SEND_SUCCESS}, NAO_RESPONDEU),
            ({FACT_FIRST_HUMAN_INBOUND}, EM_ATENDIMENTO),
            (
                {FACT_FIRST_T1_SEND_SUCCESS, FACT_FIRST_HUMAN_INBOUND},
                EM_ATENDIMENTO,
            ),
        )
        for facts, expected in cases:
            with self.subTest(facts=sorted(facts)):
                self.assertEqual(desired_status(facts), expected)

    def test_order_of_arrival_never_changes_the_answer(self) -> None:
        """Out-of-order evidence is safe: the answer depends on the set only."""
        both = [FACT_FIRST_T1_SEND_SUCCESS, FACT_FIRST_HUMAN_INBOUND]
        self.assertEqual(desired_status(set(both)), desired_status(set(reversed(both))))

    def test_unknown_facts_do_not_move_the_state(self) -> None:
        self.assertEqual(
            desired_status({"client_created_sem_atendimento"}), SEM_ATENDIMENTO
        )
        self.assertEqual(desired_status({"something_new"}), SEM_ATENDIMENTO)

    def test_only_three_transitions_are_authorised(self) -> None:
        self.assertEqual(
            ALLOWED_TRANSITIONS,
            frozenset(
                {
                    (SEM_ATENDIMENTO, NAO_RESPONDEU),
                    (SEM_ATENDIMENTO, EM_ATENDIMENTO),
                    (NAO_RESPONDEU, EM_ATENDIMENTO),
                }
            ),
        )


class EffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=1.0
        )
        self.runtime.initialize()
        self.ids = RuntimeIds(b"r" * 32, b"t" * 32)
        self.contact_key = self.ids.contact_key("5534999772714")
        self.engine = LifecycleEngine(self.runtime, self.ids, clock=lambda: 3000.0)
        self.seed()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def seed(self, *, last_proven: str = SEM_ATENDIMENTO) -> None:
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
                "VALUES (?, 'g-1', 'k', ?, 'y', 2, 1000.0, 'correlated', 1000.0)",
                (ORIGIN_TURN, self.contact_key),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO lead_lifecycles (lifecycle_id, origin_event_id, "
                "wa_turn_id, contact_key, client_id, phase, last_proven_status, "
                "created_at, updated_at) "
                "VALUES ('fx_one', ?, ?, ?, 12800, 'active', ?, 1000.0, 1000.0)",
                (origin, ORIGIN_TURN, self.contact_key, last_proven),
            )
        )

    def add_fact(self, fact_type: str, *, observed_at: float = 1100.0) -> None:
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT OR IGNORE INTO lifecycle_facts (lifecycle_id, fact_type, "
                "evidence_ref, observed_at, created_at) "
                "VALUES ('fx_one', ?, 'evidence', ?, 2000.0)",
                (fact_type, observed_at),
            )
        )

    def set_last_proven(self, status: str) -> None:
        self.runtime.write(
            lambda conn: conn.execute(
                "UPDATE lead_lifecycles SET last_proven_status = ?", (status,)
            )
        )

    def effects(self) -> list[sqlite3.Row]:
        return self.runtime.read(
            lambda conn: conn.execute(
                "SELECT * FROM lifecycle_effects ORDER BY created_at, effect_id"
            ).fetchall()
        )

    def live(self) -> list[sqlite3.Row]:
        return [row for row in self.effects() if row["state"] == "pending"]

    # ------------------------------------------------------------------

    def test_no_facts_produce_no_effect(self) -> None:
        self.engine.recompute_effects("fx_one")
        self.assertEqual(self.effects(), [])

    def test_t1_success_creates_sem_to_nao_respondeu(self) -> None:
        self.add_fact(FACT_FIRST_T1_SEND_SUCCESS)

        self.engine.recompute_effects("fx_one")

        effect = self.live()[0]
        self.assertEqual(effect["expected_status"], SEM_ATENDIMENTO)
        self.assertEqual(effect["target_status"], NAO_RESPONDEU)
        self.assertEqual(effect["cause"], FACT_FIRST_T1_SEND_SUCCESS)

    def test_human_inbound_creates_sem_to_em_atendimento(self) -> None:
        self.add_fact(FACT_FIRST_HUMAN_INBOUND)

        self.engine.recompute_effects("fx_one")

        effect = self.live()[0]
        self.assertEqual(effect["expected_status"], SEM_ATENDIMENTO)
        self.assertEqual(effect["target_status"], EM_ATENDIMENTO)

    def test_a_newer_fact_supersedes_the_pending_effect(self) -> None:
        """A human reply while Não Respondeu is still pending overtakes it."""
        self.add_fact(FACT_FIRST_T1_SEND_SUCCESS)
        self.engine.recompute_effects("fx_one")
        self.add_fact(FACT_FIRST_HUMAN_INBOUND, observed_at=1200.0)

        self.engine.recompute_effects("fx_one")

        states = {row["target_status"]: row["state"] for row in self.effects()}
        self.assertEqual(states[NAO_RESPONDEU], "superseded")
        self.assertEqual(states[EM_ATENDIMENTO], "pending")

    def test_from_nao_respondeu_a_human_creates_nao_to_em(self) -> None:
        self.set_last_proven(NAO_RESPONDEU)
        self.add_fact(FACT_FIRST_HUMAN_INBOUND)

        self.engine.recompute_effects("fx_one")

        effect = self.live()[0]
        self.assertEqual(effect["expected_status"], NAO_RESPONDEU)
        self.assertEqual(effect["target_status"], EM_ATENDIMENTO)

    def test_a_lifecycle_already_at_the_desired_status_creates_nothing(self) -> None:
        self.set_last_proven(EM_ATENDIMENTO)
        self.add_fact(FACT_FIRST_HUMAN_INBOUND)

        self.engine.recompute_effects("fx_one")

        self.assertEqual(self.effects(), [])

    def test_a_downgrade_is_never_proposed(self) -> None:
        """Manual CRM state is authoritative; nothing here may walk it back."""
        self.set_last_proven(EM_ATENDIMENTO)
        self.add_fact(FACT_FIRST_T1_SEND_SUCCESS)

        self.engine.recompute_effects("fx_one")

        self.assertEqual(self.effects(), [])

    def test_an_unmanaged_current_status_produces_no_effect(self) -> None:
        for status in ("Arquivado", "Venda Realizada", ""):
            with self.subTest(status=status):
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM lifecycle_effects")
                )
                self.set_last_proven(status)
                self.add_fact(FACT_FIRST_HUMAN_INBOUND)
                self.engine.recompute_effects("fx_one")
                self.assertEqual(self.effects(), [])

    def test_recompute_is_idempotent(self) -> None:
        self.add_fact(FACT_FIRST_T1_SEND_SUCCESS)

        self.engine.recompute_effects("fx_one")
        self.engine.recompute_effects("fx_one")
        self.engine.recompute_effects("fx_one")

        self.assertEqual(len(self.effects()), 1)

    def test_the_effect_id_is_stable_across_restarts(self) -> None:
        self.add_fact(FACT_FIRST_T1_SEND_SUCCESS)
        self.engine.recompute_effects("fx_one")
        first = self.effects()[0]["effect_id"]

        other = LifecycleEngine(self.runtime, self.ids, clock=lambda: 9999.0)
        other.recompute_effects("fx_one")

        self.assertEqual([row["effect_id"] for row in self.effects()], [first])

    def test_a_claimed_effect_is_not_superseded_silently(self) -> None:
        """A writer may already be acting on it; only Brain's own states move."""
        self.add_fact(FACT_FIRST_T1_SEND_SUCCESS)
        self.engine.recompute_effects("fx_one")
        self.runtime.write(
            lambda conn: conn.execute("UPDATE lifecycle_effects SET state = 'applied'")
        )
        self.set_last_proven(NAO_RESPONDEU)
        self.add_fact(FACT_FIRST_HUMAN_INBOUND, observed_at=1200.0)

        self.engine.recompute_effects("fx_one")

        states = sorted(row["state"] for row in self.effects())
        self.assertEqual(states, ["applied", "pending"])

    def test_a_terminal_lifecycle_produces_no_effect(self) -> None:
        self.runtime.write(
            lambda conn: conn.execute("UPDATE lead_lifecycles SET phase = 'terminal'")
        )
        self.add_fact(FACT_FIRST_HUMAN_INBOUND)

        self.engine.recompute_effects("fx_one")

        self.assertEqual(self.effects(), [])


if __name__ == "__main__":
    unittest.main()
