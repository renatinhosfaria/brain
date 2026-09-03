from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.meta_ads_models import ConfirmedMetaAttribution, ObservedCtwaSource
from brain.meta_ads_store import MetaAdsStore
from brain.runtime_db import RuntimeDatabase


class MetaAdsStoreTests(unittest.TestCase):
    account_id = "act_1598606388477916"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=0.25
        )
        self.runtime.initialize()
        self.store = MetaAdsStore(self.account_id)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def insert_event(conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "INSERT INTO transport_events "
            "(event_id, observer_device_id, direction, received_at, transport_kind, "
            "created_at) VALUES (?, 'observer', 'inbound', 1.0, 'ctwa_candidate', 1.0)",
            (event_id,),
        )

    @staticmethod
    def confirmed() -> ConfirmedMetaAttribution:
        return ConfirmedMetaAttribution(
            "101",
            "Summer Ad",
            "202",
            "Summer Campaign",
            "PAUSED",
            "ACTIVE",
            "ACTIVE",
            "ACTIVE",
        )

    def stage(
        self, event_id: str, source_id: str = "101", clid: str | None = "clid"
    ) -> None:
        def write(conn: sqlite3.Connection) -> None:
            self.insert_event(conn, event_id)
            self.store.stage_event(
                conn, event_id, ObservedCtwaSource(source_id, clid), 10.0
            )

        self.runtime.write(write)

    def claim(self, source_id: str = "101", now: float = 10.0) -> str:
        token = self.runtime.write(
            lambda conn: self.store.claim_source_job(conn, source_id, now, 30.0)
        )
        self.assertIsNotNone(token)
        return str(token)

    def test_stage_is_idempotent_but_rejects_source_or_clid_replay_conflicts(
        self,
    ) -> None:
        self.stage("event-1")

        self.runtime.write(
            lambda conn: self.store.stage_event(
                conn, "event-1", ObservedCtwaSource("101", "clid"), 20.0
            )
        )
        for observed in (
            ObservedCtwaSource("102", "clid"),
            ObservedCtwaSource("101", None),
        ):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                self.runtime.write(
                    lambda conn, observed=observed: self.store.stage_event(
                        conn, "event-1", observed, 20.0
                    )
                )

        counts = self.runtime.read(
            lambda conn: tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("ctwa_meta_attributions", "meta_attribution_jobs")
            )
        )
        self.assertEqual(counts, (1, 1))

    def test_stage_deduplicates_jobs_by_original_source_id(self) -> None:
        self.stage("event-1", clid="one")
        self.stage("event-2", clid="two")

        jobs = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT account_id, source_id FROM meta_attribution_jobs"
            ).fetchall()
        )
        self.assertEqual([tuple(row) for row in jobs], [(self.account_id, "101")])

    def test_complete_confirms_every_pending_event_without_overwriting_snapshots(
        self,
    ) -> None:
        self.stage("event-1")
        self.stage("event-2")
        token = self.claim()
        updated = self.runtime.write(
            lambda conn: self.store.complete_source(
                conn, "101", self.confirmed(), 20.0, token
            )
        )
        self.assertEqual(updated, 2)

        self.runtime.write(
            lambda conn: conn.execute(
                "UPDATE ctwa_meta_attributions SET ad_name = 'Original snapshot' "
                "WHERE event_id = 'event-1'"
            )
        )
        self.stage("event-3")
        token = self.claim(now=30.0)
        self.runtime.write(
            lambda conn: self.store.complete_source(
                conn,
                "101",
                ConfirmedMetaAttribution(
                    "101",
                    "Replacement",
                    "202",
                    "Replacement Campaign",
                    "ACTIVE",
                    "ACTIVE",
                    "ACTIVE",
                    "ACTIVE",
                ),
                40.0,
                token,
            )
        )

        names = self.runtime.read(
            lambda conn: [
                tuple(row)
                for row in conn.execute(
                    "SELECT event_id, status, ad_name FROM ctwa_meta_attributions "
                    "ORDER BY event_id"
                )
            ]
        )
        self.assertEqual(
            names,
            [
                ("event-1", "confirmed", "Original snapshot"),
                ("event-2", "confirmed", "Summer Ad"),
                ("event-3", "confirmed", "Replacement"),
            ],
        )

    def test_complete_rejects_a_remote_ad_id_that_is_not_the_original_source_id(
        self,
    ) -> None:
        self.stage("event-1")
        token = self.claim()
        mismatched = ConfirmedMetaAttribution(
            "999",
            "Wrong Ad",
            "202",
            "Summer Campaign",
            "ACTIVE",
            "ACTIVE",
            "ACTIVE",
            "ACTIVE",
        )
        with self.assertRaises(ValueError):
            self.runtime.write(
                lambda conn: self.store.complete_source(
                    conn, "101", mismatched, 20.0, token
                )
            )
        row = self.runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT status, ad_id FROM ctwa_meta_attributions"
                ).fetchone()
            )
        )
        self.assertEqual(row, ("pending", None))

    def test_claim_uses_a_lease_token_and_allows_only_expired_lease_to_be_reclaimed(
        self,
    ) -> None:
        self.stage("event-1")
        first = self.claim(now=10.0)
        self.assertIsNone(
            self.runtime.write(
                lambda conn: self.store.claim_source_job(conn, "101", 20.0, 30.0)
            )
        )
        second = self.runtime.write(
            lambda conn: self.store.claim_source_job(conn, "101", 40.0, 30.0)
        )
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertEqual(
            self.runtime.write(
                lambda conn: self.store.complete_source(
                    conn, "101", self.confirmed(), 41.0, first
                )
            ),
            0,
        )

    def test_transient_failures_follow_bounded_retry_schedule(self) -> None:
        self.stage("event-1")
        now = 10.0
        expected_delays = (60.0, 300.0, 900.0, 3600.0, 21600.0, 86400.0, 86400.0)
        for delay in expected_delays:
            token = self.claim(now=now)
            self.assertTrue(
                self.runtime.write(
                    lambda conn, token=token, now=now: self.store.fail_source(
                        conn, "101", "meta_timeout", now, token
                    )
                )
            )
            due_at = self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT next_attempt_at FROM meta_attribution_jobs"
                ).fetchone()[0]
            )
            self.assertEqual(due_at, now + delay)
            now = due_at

    def test_terminal_failures_mark_unavailable_and_remove_the_job(self) -> None:
        self.stage("event-1")
        token = self.claim()
        self.assertTrue(
            self.runtime.write(
                lambda conn: self.store.fail_source(
                    conn, "101", "meta_not_found", 20.0, token
                )
            )
        )
        self.assertEqual(
            self.runtime.read(
                lambda conn: tuple(
                    conn.execute(
                        "SELECT status, reason_code FROM ctwa_meta_attributions"
                    ).fetchone()
                )
            ),
            ("unavailable", "meta_not_found"),
        )
        self.assertEqual(
            self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM meta_attribution_jobs"
                ).fetchone()[0]
            ),
            0,
        )

    def test_rejects_unbounded_reason_codes_before_writing_them(self) -> None:
        self.stage("event-1")
        token = self.claim()
        with self.assertRaises(ValueError):
            self.runtime.write(
                lambda conn: self.store.fail_source(
                    conn, "101", "remote returned secret=abc", 20.0, token
                )
            )
        reason = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT reason_code FROM ctwa_meta_attributions"
            ).fetchone()[0]
        )
        self.assertIsNone(reason)

    def test_auth_circuit_defers_due_jobs_and_closes_on_success(self) -> None:
        self.stage("event-1")
        until = self.runtime.write(
            lambda conn: self.store.open_auth_circuit(conn, 10.0, 0.0)
        )
        self.assertEqual(until, 70.0)
        self.assertEqual(
            self.runtime.read(lambda conn: self.store.due_source_ids(conn, 20.0, 10)),
            [],
        )
        self.assertIsNone(
            self.runtime.write(
                lambda conn: self.store.claim_source_job(conn, "101", 20.0, 30.0)
            )
        )
        self.runtime.write(lambda conn: self.store.close_auth_circuit(conn, 20.0))
        self.assertEqual(
            self.runtime.read(lambda conn: self.store.due_source_ids(conn, 20.0, 10)),
            ["101"],
        )

    def test_context_exposes_confirmed_values_or_bounded_pending_reason_only(
        self,
    ) -> None:
        self.stage("event-1")
        pending = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "event-1")
        )
        self.assertEqual(pending, {"status": "pending"})
        token = self.claim()
        self.runtime.write(
            lambda conn: self.store.complete_source(
                conn, "101", self.confirmed(), 20.0, token
            )
        )
        confirmed = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "event-1")
        )
        self.assertEqual(
            confirmed,
            {
                "status": "confirmed",
                "ad_id": "101",
                "ad_name": "Summer Ad",
                "campaign_id": "202",
                "campaign_name": "Summer Campaign",
            },
        )

    def test_purge_removes_orphaned_jobs_after_parent_cascade(self) -> None:
        self.stage("event-1")
        self.runtime.write(
            lambda conn: conn.execute(
                "DELETE FROM transport_events WHERE event_id = 'event-1'"
            )
        )
        removed = self.runtime.write(lambda conn: self.store.purge_expired(conn, 100.0))
        self.assertEqual(removed, 1)
        counts = self.runtime.read(
            lambda conn: tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("ctwa_meta_attributions", "meta_attribution_jobs")
            )
        )
        self.assertEqual(counts, (0, 0))


if __name__ == "__main__":
    unittest.main()
