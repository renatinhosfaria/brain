from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.hermes_evidence import BoundTask, TerminalRun
from brain.lifecycle_engine import LifecycleEngine
from brain.runtime_db import RuntimeDatabase
from brain.transport_models import RuntimeIds

ORIGIN_TURN = "waturn_" + "a" * 64
OTHER_TURN = "waturn_" + "b" * 64


class LifecycleBindingTests(unittest.TestCase):
    CONTACT_PHONE = "5534999772714"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=1.0
        )
        self.runtime.initialize()
        self.ids = RuntimeIds(b"r" * 32, b"t" * 32)
        self.contact_key = self.ids.contact_key(self.CONTACT_PHONE)
        self.engine = LifecycleEngine(self.runtime, self.ids, clock=lambda: 2000.0)
        self._events = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------
    # Fixtures

    def add_event(
        self,
        key_id: str,
        *,
        kind: str = "ctwa_candidate",
        timestamp: float = 1000.0,
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

    def add_turn(
        self,
        wa_turn_id: str = ORIGIN_TURN,
        *,
        status: str = "correlated",
        events: tuple[str, ...] = (),
        contact_key: str | None = None,
    ) -> None:
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, 'g-1', 'x', ?, 'y', 2, 1000.0, ?, 1000.0)",
                (wa_turn_id, contact_key or self.contact_key, status),
            )
        )
        for ordinal, event_id in enumerate(events):
            self.runtime.write(
                lambda conn, e=event_id, o=ordinal: conn.execute(
                    "INSERT INTO turn_events (wa_turn_id, event_id, ordinal) "
                    "VALUES (?, ?, ?)",
                    (wa_turn_id, e, o),
                )
            )

    @staticmethod
    def cadastro_task(
        wa_turn_id: str = ORIGIN_TURN, *, task_id: str = "t_1", run_id: int = 1
    ) -> BoundTask:
        return BoundTask(
            task_id=task_id,
            assignee="cadastro",
            status="done",
            current_run_id=run_id,
            session_id="s-1",
            wa_turn_id=wa_turn_id,
            stage="cadastro",
        )

    @staticmethod
    def cadastro_run(
        *,
        decision: str = "LEAD_NOVO_CADASTRADO",
        client_id: object = 12800,
        run_id: int = 1,
        task_id: str = "t_1",
    ) -> TerminalRun:
        entities = {} if client_id is None else {"client_id": client_id}
        return TerminalRun(
            run_id=run_id,
            task_id=task_id,
            status="done",
            summary=f"{decision} cliente_id={client_id}",
            started_at=1000.0,
            ended_at=1010.0,
            decision=decision,
            entities=entities,
        )

    def fact_types(self) -> list[str]:
        """Fact types other than the creation fact every lifecycle carries."""
        return [
            str(row["fact_type"])
            for row in self.rows("lifecycle_facts")
            if row["fact_type"] != "client_created_sem_atendimento"
        ]

    def rows(self, table: str) -> list[sqlite3.Row]:
        return self.runtime.read(
            lambda conn: conn.execute(f"SELECT * FROM {table}").fetchall()
        )

    def bind(self, task=None, run=None):
        return self.engine.bind_completed_cadastro(
            task or self.cadastro_task(), run or self.cadastro_run()
        )

    # ------------------------------------------------------------------
    # Binding

    def test_binds_the_ctwa_origin_of_the_correlated_turn(self) -> None:
        origin = self.add_event("3EB0CTWA")
        self.add_turn(events=(origin,))

        result = self.bind()

        self.assertEqual(result.status, "created")
        lifecycle = self.rows("lead_lifecycles")[0]
        self.assertEqual(lifecycle["origin_event_id"], origin)
        self.assertEqual(lifecycle["wa_turn_id"], ORIGIN_TURN)
        self.assertEqual(lifecycle["client_id"], 12800)
        self.assertEqual(lifecycle["contact_key"], self.contact_key)
        self.assertEqual(lifecycle["phase"], "active")

    def test_replay_is_a_noop(self) -> None:
        self.add_turn(events=(self.add_event("3EB0CTWA"),))
        first = self.bind()

        second = self.bind()

        self.assertEqual(first.status, "created")
        self.assertEqual(second.status, "noop")
        self.assertEqual(len(self.rows("lead_lifecycles")), 1)

    def test_same_origin_with_a_different_client_is_a_hard_conflict(self) -> None:
        self.add_turn(events=(self.add_event("3EB0CTWA"),))
        self.bind()

        result = self.bind(run=self.cadastro_run(client_id=99999))

        self.assertEqual(result.status, "conflict")
        self.assertEqual(len(self.rows("lead_lifecycles")), 1)
        self.assertEqual(self.rows("lead_lifecycles")[0]["client_id"], 12800)

    def test_non_creating_decisions_produce_no_lifecycle(self) -> None:
        for decision in ("JA_E_CLIENTE", "CORRETOR_ATIVO", "INCONCLUSIVO"):
            with self.subTest(decision=decision):
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM lead_lifecycles")
                )
                result = self.bind(run=self.cadastro_run(decision=decision))
                self.assertEqual(result.status, "skipped")
                self.assertEqual(self.rows("lead_lifecycles"), [])

    def test_client_id_must_be_a_positive_integer(self) -> None:
        self.add_turn(events=(self.add_event("3EB0CTWA"),))
        for bad in (None, 0, -1, "12800", 1.5, True):
            with self.subTest(client_id=bad):
                result = self.bind(run=self.cadastro_run(client_id=bad))
                self.assertEqual(result.status, "skipped")
                self.assertEqual(self.rows("lead_lifecycles"), [])

    # ------------------------------------------------------------------
    # The P7 regression: the turn must actually be correlated

    def test_uncorrelated_turn_creates_no_lifecycle(self) -> None:
        """A Cadastro card on a turn with no proven transport origin binds nothing.

        This is premise P7: before the amendment the Cadastro card carried a
        Kanban-notification turn, which has no transport event and never will.
        """
        for status in ("pending", "uncorrelatable"):
            with self.subTest(status=status):
                self.runtime.write(lambda conn: conn.execute("DELETE FROM turn_events"))
                self.runtime.write(
                    lambda conn: conn.execute("DELETE FROM whatsapp_turns")
                )
                self.add_turn(status=status, events=())
                result = self.bind()
                self.assertEqual(result.status, "skipped")
                self.assertEqual(self.rows("lead_lifecycles"), [])

    def test_missing_turn_creates_no_lifecycle(self) -> None:
        result = self.bind(task=self.cadastro_task(OTHER_TURN))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.rows("lead_lifecycles"), [])

    def test_turn_without_a_ctwa_candidate_creates_no_lifecycle(self) -> None:
        ordinary = self.add_event("3EB0PLAIN", kind="ordinary_inbound")
        self.add_turn(events=(ordinary,))

        result = self.bind()

        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.rows("lead_lifecycles"), [])

    def test_two_ctwa_candidates_in_one_turn_fail_closed(self) -> None:
        first = self.add_event("3EB0ONE")
        second = self.add_event("3EB0TWO", timestamp=1001.0)
        self.add_turn(events=(first, second))

        result = self.bind()

        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.rows("lead_lifecycles"), [])

    # ------------------------------------------------------------------
    # Human fact already present at bind time

    def test_ordinary_event_after_origin_materialises_the_human_fact(self) -> None:
        origin = self.add_event("3EB0CTWA", timestamp=1000.0)
        self.add_turn(events=(origin,))
        self.add_event("3EB0HUMAN", kind="ordinary_inbound", timestamp=1005.0)

        self.bind()

        self.assertEqual(self.fact_types(), ["first_human_inbound"])

    def test_a_later_ctwa_event_is_not_a_human_fact(self) -> None:
        origin = self.add_event("3EB0CTWA", timestamp=1000.0)
        self.add_turn(events=(origin,))
        self.add_event("3EB0SECONDAD", kind="ctwa_candidate", timestamp=1005.0)

        self.bind()

        self.assertEqual(self.fact_types(), [])

    def test_events_before_the_origin_are_not_human_facts(self) -> None:
        earlier = self.add_event(
            "3EB0EARLIER", kind="ordinary_inbound", timestamp=900.0
        )
        origin = self.add_event("3EB0CTWA", timestamp=1000.0)
        self.add_turn(events=(origin,))
        self.assertNotEqual(earlier, origin)

        self.bind()

        self.assertEqual(self.fact_types(), [])

    def test_another_contact_event_is_not_a_human_fact(self) -> None:
        origin = self.add_event("3EB0CTWA", timestamp=1000.0)
        self.add_turn(events=(origin,))
        self.add_event(
            "3EB0OTHER",
            kind="ordinary_inbound",
            timestamp=1005.0,
            contact_key=self.ids.contact_key("5534999000000"),
        )

        self.bind()

        self.assertEqual(self.fact_types(), [])

    # ------------------------------------------------------------------
    # Restart safety

    def test_watermark_advances_only_after_durable_processing(self) -> None:
        self.add_turn(events=(self.add_event("3EB0CTWA"),))
        self.assertEqual(self.engine.kanban_watermark(), 0)

        self.bind(task=self.cadastro_task(run_id=7), run=self.cadastro_run(run_id=7))

        self.assertEqual(self.engine.kanban_watermark(), 7)

    def test_watermark_never_moves_backwards(self) -> None:
        self.add_turn(events=(self.add_event("3EB0CTWA"),))
        self.bind(task=self.cadastro_task(run_id=9), run=self.cadastro_run(run_id=9))

        self.bind(
            task=self.cadastro_task(run_id=4, task_id="t_old"),
            run=self.cadastro_run(run_id=4, task_id="t_old", decision="JA_E_CLIENTE"),
        )

        self.assertEqual(self.engine.kanban_watermark(), 9)

    def test_lifecycle_persistence_contains_no_raw_identity(self) -> None:
        self.add_turn(events=(self.add_event("3EB0CTWA"),))
        self.bind()

        serialized = json.dumps(
            [dict(row) for row in self.rows("lead_lifecycles")], default=str
        )
        for raw in (self.CONTACT_PHONE, "3EB0CTWA"):
            self.assertNotIn(raw, serialized)


if __name__ == "__main__":
    unittest.main()
