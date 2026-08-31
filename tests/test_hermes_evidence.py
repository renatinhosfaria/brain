from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.db import SCHEMA_REQUIREMENTS, ReadOnlyDatabase, SchemaGuard
from brain.hermes_evidence import HermesEvidenceReader

ORIGIN_TURN = "waturn_" + "a" * 64

KANBAN_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    assignee TEXT,
    status TEXT,
    current_run_id INTEGER,
    session_id TEXT,
    idempotency_key TEXT,
    created_at REAL
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY,
    task_id TEXT,
    status TEXT,
    summary TEXT,
    metadata TEXT,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE kanban_notify_subs (
    task_id TEXT, platform TEXT, chat_id TEXT, chat_type TEXT,
    notifier_profile TEXT
);
"""

STATE_SCHEMA = """
CREATE TABLE delivery_obligations (
    obligation_id TEXT PRIMARY KEY,
    session_key TEXT,
    platform TEXT,
    chat_id TEXT,
    content TEXT,
    state TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, session_key TEXT, source TEXT, chat_id TEXT,
    chat_type TEXT, started_at REAL, ended_at REAL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
    timestamp REAL, active INTEGER, compacted INTEGER, display_kind TEXT,
    _compressed_summary TEXT, tool_calls TEXT, tool_name TEXT
);
"""


class HermesEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.kanban_path = self.root / "kanban.db"
        self.state_path = self.root / "state.db"
        self.build(self.kanban_path, KANBAN_SCHEMA)
        self.build(self.state_path, STATE_SCHEMA)
        self.reader = HermesEvidenceReader(self.state_db(), self.kanban_db())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def build(path: Path, schema: str) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(schema)
            conn.commit()
        finally:
            conn.close()

    def kanban_db(self) -> ReadOnlyDatabase:
        return ReadOnlyDatabase(self.kanban_path, retries=0, timeout_seconds=1.0)

    def state_db(self) -> ReadOnlyDatabase:
        return ReadOnlyDatabase(self.state_path, retries=0, timeout_seconds=1.0)

    def write(self, path: Path, sql: str, values: tuple) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(sql, values)
            conn.commit()
        finally:
            conn.close()

    def add_task(
        self,
        task_id: str = "t_1",
        *,
        assignee: str = "cadastro",
        status: str = "done",
        run_id: int | None = 1,
        idempotency_key: str = f"whatsapp:{ORIGIN_TURN}:cadastro",
        session_id: str = "s-1",
    ) -> None:
        self.write(
            self.kanban_path,
            "INSERT INTO tasks (id, assignee, status, current_run_id, session_id, "
            "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, assignee, status, run_id, session_id, idempotency_key, 1000.0),
        )

    def add_run(
        self,
        run_id: int = 1,
        *,
        task_id: str = "t_1",
        status: str = "done",
        metadata: object = None,
        summary: str = "LEAD_NOVO_CADASTRADO cliente_id=12800",
        ended_at: float | None = 1010.0,
    ) -> None:
        if metadata is None:
            metadata = {
                "status": "completed",
                "decision": "LEAD_NOVO_CADASTRADO",
                "entities": {"client_id": 12800},
                "requested_next_action": "return_to_ceo",
            }
        encoded = metadata if isinstance(metadata, str) else json.dumps(metadata)
        self.write(
            self.kanban_path,
            "INSERT INTO task_runs (id, task_id, status, summary, metadata, "
            "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, status, summary, encoded, 1000.0, ended_at),
        )

    def add_obligation(
        self,
        obligation_id: str = "o_1",
        *,
        session_key: str = "wa:g",
        state: str = "delivered",
        content: str = "Olá, tudo bem?",
        updated_at: float = 1020.0,
    ) -> None:
        self.write(
            self.state_path,
            "INSERT INTO delivery_obligations (obligation_id, session_key, platform, "
            "chat_id, content, state, created_at, updated_at) "
            "VALUES (?, ?, 'whatsapp', 'c-1', ?, ?, ?, ?)",
            (obligation_id, session_key, content, state, 1015.0, updated_at),
        )

    # ------------------------------------------------------------------
    # Schema guards

    def test_lifecycle_columns_are_required_by_the_schema_guard(self) -> None:
        """Missing evidence columns must disable lifecycle, not fail at query time."""
        self.assertLessEqual(
            {"idempotency_key"}, SCHEMA_REQUIREMENTS["kanban"]["tasks"]
        )
        self.assertLessEqual(
            {"summary", "metadata", "started_at", "ended_at"},
            SCHEMA_REQUIREMENTS["kanban"]["task_runs"],
        )
        self.assertLessEqual(
            {"obligation_id", "session_key", "content", "state", "updated_at"},
            SCHEMA_REQUIREMENTS["state"]["delivery_obligations"],
        )

    def test_guard_fails_when_any_required_column_is_absent(self) -> None:
        for table, column in (
            ("tasks", "idempotency_key"),
            ("task_runs", "metadata"),
        ):
            with self.subTest(table=table, column=column):
                path = self.root / f"partial-{table}-{column}.db"
                schema = KANBAN_SCHEMA.replace(f"    {column} TEXT,\n", "")
                schema = schema.replace(f"    {column} REAL,\n", "")
                self.build(path, schema)
                guard = SchemaGuard(
                    self.state_db(),
                    ReadOnlyDatabase(path, retries=0, timeout_seconds=1.0),
                )
                self.assertFalse(guard.check())

    def test_guard_accepts_the_complete_fixture(self) -> None:
        self.assertTrue(SchemaGuard(self.state_db(), self.kanban_db()).check())

    # ------------------------------------------------------------------
    # Evidence readers

    def test_lists_only_tasks_bound_by_a_whatsapp_idempotency_key(self) -> None:
        self.add_task("t_1", idempotency_key=f"whatsapp:{ORIGIN_TURN}:cadastro")
        self.add_task("t_2", idempotency_key="telegram:other:cadastro")
        self.add_task("t_3", idempotency_key=None)

        bound = self.reader.list_bound_tasks()

        self.assertEqual([task.task_id for task in bound], ["t_1"])
        self.assertEqual(bound[0].wa_turn_id, ORIGIN_TURN)
        self.assertEqual(bound[0].stage, "cadastro")

    def test_terminal_run_returns_structured_metadata(self) -> None:
        self.add_task()
        self.add_run()

        run = self.reader.terminal_run("t_1", 1)

        self.assertIsNotNone(run)
        self.assertEqual(run.decision, "LEAD_NOVO_CADASTRADO")
        self.assertEqual(run.entities["client_id"], 12800)

    def test_malformed_metadata_is_unavailable_not_partial_truth(self) -> None:
        for label, metadata in (
            ("not json", "LEAD_NOVO_CADASTRADO cliente 12800"),
            ("json array", "[1, 2, 3]"),
            ("json scalar", '"done"'),
            ("empty", ""),
        ):
            with self.subTest(label=label):
                path = self.root / f"kanban-{label.replace(' ', '-')}.db"
                self.build(path, KANBAN_SCHEMA)
                self.kanban_path, original = path, self.kanban_path
                self.add_task()
                self.add_run(metadata=metadata)
                reader = HermesEvidenceReader(self.state_db(), self.kanban_db())
                run = reader.terminal_run("t_1", 1)
                self.kanban_path = original
                self.assertIsNotNone(run, "the run itself is readable")
                self.assertIsNone(run.decision)
                self.assertEqual(run.entities, {})

    def test_run_must_belong_to_its_task_and_be_terminal(self) -> None:
        self.add_task()
        self.add_run(run_id=1, task_id="t_other")
        self.assertIsNone(self.reader.terminal_run("t_1", 1))

        self.add_run(run_id=2, task_id="t_1", status="running", ended_at=None)
        self.assertIsNone(self.reader.terminal_run("t_1", 2))

    def test_delivered_obligations_filter_by_session_and_state(self) -> None:
        self.add_obligation("o_1", state="delivered", session_key="wa:g")
        self.add_obligation("o_2", state="pending", session_key="wa:g")
        self.add_obligation("o_3", state="delivered", session_key="wa:other")

        delivered = self.reader.delivered_obligations("wa:g", since=1000.0)

        self.assertEqual([o.obligation_id for o in delivered], ["o_1"])
        self.assertEqual(delivered[0].content, "Olá, tudo bem?")

    def test_delivered_obligations_respect_the_time_window(self) -> None:
        self.add_obligation("o_old", updated_at=900.0)
        self.add_obligation("o_new", updated_at=1100.0)

        delivered = self.reader.delivered_obligations("wa:g", since=1000.0)

        self.assertEqual([o.obligation_id for o in delivered], ["o_new"])

    def test_readers_never_write_to_hermes(self) -> None:
        self.assertFalse(hasattr(self.reader.state, "write"))
        self.assertFalse(hasattr(self.reader.kanban, "write"))


if __name__ == "__main__":
    unittest.main()
