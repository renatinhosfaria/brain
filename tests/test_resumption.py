"""Synthetic Telegram-control/WhatsApp-context regression tests; no live DBs."""

import sqlite3
import time
import unittest
from unittest.mock import patch

import test_brain

from brain.errors import BrainError


class ResumptionTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_brain.BrainFixture()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        with sqlite3.connect(self.fx.state_path) as conn:
            conn.execute(
                "INSERT INTO sessions VALUES ('control', 'tg:control', 'telegram', 'synthetic-group', 'group', 3)"
            )
        with sqlite3.connect(self.fx.kanban_path) as conn:
            conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
            conn.execute(
                "INSERT INTO tasks VALUES ('parent', 'reno', 'done', NULL, 'a-new', NULL)"
            )
            conn.execute(
                "INSERT INTO kanban_notify_subs VALUES ('parent', 'whatsapp', '5511999990000@s.whatsapp.net', 'dm', 'default')"
            )
            conn.execute("INSERT INTO task_links VALUES ('parent', 'task-a')")
            conn.execute(
                "UPDATE tasks SET session_id='control', status='blocked', current_run_id=NULL WHERE id='task-a'"
            )
            conn.execute("UPDATE task_runs SET status='blocked' WHERE id=101")

    def resume_run(self, run_id=106):
        with sqlite3.connect(self.fx.kanban_path) as conn:
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id='task-a'",
                (run_id,),
            )
            conn.execute(
                "INSERT INTO task_runs (id, task_id, status, started_at) VALUES (?, 'task-a', 'running', ?)",
                (run_id, __import__("time").time()),
            )

    def recent(self, run_id=106, arguments=None):
        return self.fx.service.call_tool(
            "conversation_recent",
            arguments or {},
            self.fx.headers(task="task-a", run=str(run_id)),
        )

    def test_explicit_admin_grant_resumes_same_task_without_rewriting_origin(self):
        from brain.resumption import ResumptionRegistry

        registry = ResumptionRegistry(self.fx.settings)
        registry.issue("task-a", "parent", "f922bc7e-4d07-4053-937c-4179f2ae649c")
        self.resume_run()
        result = self.recent()
        self.assertIn("fato A", str(result))
        self.assertNotIn("segredo B", str(result))
        with sqlite3.connect(self.fx.kanban_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT session_id FROM tasks WHERE id='task-a'"
                ).fetchone()[0],
                "control",
            )
        self.assertEqual(self.recent(), result)

    def issue(self):
        from brain.resumption import ResumptionRegistry

        self.registry = ResumptionRegistry(self.fx.settings)
        return self.registry.issue(
            "task-a", "parent", "f922bc7e-4d07-4053-937c-4179f2ae649c"
        )

    def test_grant_cannot_skip_first_run_even_if_it_never_requested_history(self):
        self.issue()
        self.resume_run(106)
        with sqlite3.connect(self.fx.kanban_path) as conn:
            conn.execute("UPDATE task_runs SET status='failed' WHERE id=106")
        self.resume_run(107)
        with self.assertRaises(BrainError):
            self.recent(107)

    def test_concurrent_reads_bind_once_and_survive_new_service(self):
        from concurrent.futures import ThreadPoolExecutor

        from brain.service import BrainService

        self.issue()
        self.resume_run()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.recent(), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.registry.status("task-a")["bound_run_id"], 106)
        service = BrainService(self.fx.settings)
        self.addCleanup(service.close)
        self.assertEqual(
            service.call_tool("conversation_recent", {}, self.fx.headers(run="106")),
            results[0],
        )

    def test_inspection_is_read_only_and_missing_authorization_is_rejected(self):
        from brain.resumption import ResumptionRegistry

        registry = ResumptionRegistry(self.fx.settings)
        with patch.object(
            registry.runtime, "write", side_effect=AssertionError("unexpected write")
        ):
            binding, _ = registry.inspect("task-a", "parent")
            self.assertEqual(binding["control_session_id"], "control")
            for value in (
                None,
                "",
                "operator said yes",
                "F922BC7E-4D07-4053-937C-4179F2AE649C",
            ):
                with self.subTest(value=value), self.assertRaises(BrainError):
                    registry.issue("task-a", "parent", value)
        with sqlite3.connect(self.fx.kanban_path) as conn:
            conn.execute("DELETE FROM task_links")
        with self.assertRaises(BrainError):
            registry.issue("task-a", "parent", "f922bc7e-4d07-4053-937c-4179f2ae649c")

    def test_native_integer_timestamp_in_same_second_is_valid(self):
        with patch("brain.resumption.time.time", return_value=2000.75):
            self.issue()
            self.resume_run()
            with sqlite3.connect(self.fx.kanban_path) as conn:
                conn.execute("UPDATE task_runs SET started_at=2000 WHERE id=106")
            self.assertIn("fato A", str(self.recent()))

    def test_missing_or_unavailable_runtime_fails_closed_without_creation(self):
        from dataclasses import replace

        self.resume_run()
        missing = self.fx.runtime_path.parent / "never-created.db"
        self.fx.service.authorizer.settings = replace(
            self.fx.settings, runtime_db=missing
        )
        with self.assertRaises(BrainError):
            self.recent()
        self.assertFalse(missing.exists())
        self.fx.service.authorizer.settings = self.fx.settings
        with (
            patch(
                "brain.resumption.RuntimeDatabase.write",
                side_effect=sqlite3.OperationalError("fixture unavailable"),
            ),
            self.assertRaises(BrainError),
        ):
            self.recent()

    def test_no_grant_fails_closed(self):
        self.resume_run()
        with self.assertRaises(BrainError):
            self.recent()

    def test_arbitrary_identity_argument_denied(self):
        self.issue()
        self.resume_run()
        for key in (
            "session_id",
            "chat_id",
            "parent_id",
            "authorization_id",
            "profile",
        ):
            with self.subTest(key=key), self.assertRaises(BrainError):
                self.recent(arguments={key: "arbitrary"})

    def test_mutated_binding_and_cross_contact_denied(self):
        self.issue()
        self.resume_run()
        mutations = [
            (
                self.fx.kanban_path,
                "UPDATE tasks SET session_id='b-one' WHERE id='parent'",
            ),
            (
                self.fx.kanban_path,
                "UPDATE tasks SET assignee='famaagent' WHERE id='task-a'",
            ),
            (self.fx.kanban_path, "DELETE FROM task_links"),
            (
                self.fx.kanban_path,
                "UPDATE tasks SET status='running' WHERE id='parent'",
            ),
            (
                self.fx.kanban_path,
                "UPDATE kanban_notify_subs SET chat_id='5511888880000@s.whatsapp.net' WHERE task_id IN ('task-a','parent')",
            ),
            (
                self.fx.state_path,
                "UPDATE sessions SET session_key='tg:changed' WHERE id='control'",
            ),
            (
                self.fx.state_path,
                "UPDATE sessions SET session_key='wa:a' WHERE id='b-one'",
            ),
        ]
        for db, sql in mutations:
            with self.subTest(sql=sql):
                conn = sqlite3.connect(db)
                before = conn.serialize()
                conn.execute(sql)
                conn.commit()
                try:
                    with self.assertRaises(BrainError):
                        self.recent()
                finally:
                    # Synthetic fixture only: restore each independent mutation.
                    conn.deserialize(before)
                    backup = sqlite3.connect(db)
                    conn.backup(backup)
                    backup.close()
                    conn.close()

    def test_expiry_revocation_and_other_run_deny(self):
        self.issue()
        self.resume_run()
        self.recent()
        with (
            patch("brain.resumption.time.time", return_value=time.time() + 3601),
            self.assertRaises(BrainError),
        ):
            self.recent()
        self.resume_run(107)
        with self.assertRaises(BrainError):
            self.recent(107)
        self.registry.revoke("task-a")
        self.assertIsNotNone(self.registry.status("task-a")["revoked_at"])
        with self.assertRaises(BrainError):
            self.recent(107)

    def test_admin_cli_requires_local_operator_and_explicit_attestation(self):
        import io
        from contextlib import redirect_stdout

        from brain.resumption_admin import main

        with patch(
            "brain.resumption_admin.BrainSettings.from_env",
            return_value=self.fx.settings,
        ):
            for uid, env, extra in (
                (1000, {}, ["--apply", "--attest-authenticated-control"]),
                (
                    0,
                    {"HERMES_KANBAN_TASK": "worker"},
                    ["--apply", "--attest-authenticated-control"],
                ),
                (0, {}, ["--apply"]),
            ):
                with (
                    self.subTest(uid=uid, env=env, extra=extra),
                    patch("os.geteuid", return_value=uid),
                    patch.dict("os.environ", env, clear=True),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        main(
                            [
                                "inspect",
                                "task-a",
                                "--parent",
                                "parent",
                                "--authorization-id",
                                "f922bc7e-4d07-4053-937c-4179f2ae649c",
                                *extra,
                            ]
                        ),
                        1,
                    )
            with (
                patch("os.geteuid", return_value=0),
                patch.dict("os.environ", {}, clear=True),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        main(["inspect", "task-a", "--parent", "parent"]), 0
                    )
                self.assertNotIn("5511", output.getvalue())
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        main(
                            [
                                "inspect",
                                "task-a",
                                "--parent",
                                "parent",
                                "--authorization-id",
                                "f922bc7e-4d07-4053-937c-4179f2ae649c",
                                "--apply",
                                "--attest-authenticated-control",
                            ]
                        ),
                        0,
                    )
        self.resume_run()
        self.assertIn("fato A", str(self.recent()))

    def test_issuance_is_idempotent_not_renewable(self):
        self.assertEqual(self.issue()["status"], "issued")
        before = self.registry.status("task-a")
        self.assertEqual(self.issue()["status"], "already_issued")
        self.assertEqual(self.registry.status("task-a"), before)
        with self.assertRaises(BrainError):
            self.registry.issue(
                "task-a", "parent", "00000000-0000-0000-0000-000000000001"
            )
        with (
            patch("brain.resumption.time.time", return_value=time.time() + 3601),
            self.assertRaises(BrainError),
        ):
            self.issue()
