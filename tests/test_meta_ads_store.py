from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.meta_ads_models import MetaAdRecord, ObservedAttribution
from brain.meta_ads_store import MetaAdsStore
from brain.runtime_db import RuntimeDatabase

ACCOUNT_ID = "1598606388477916"


class MetaAdsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=0.25
        )
        self.runtime.initialize()
        self.store = MetaAdsStore(ACCOUNT_ID)
        self.observed = ObservedAttribution("120200000000001", "clid-one")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _event(conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "INSERT INTO transport_events "
            "(event_id, observer_device_id, direction, received_at, transport_kind, "
            "created_at) VALUES (?, 'observer-a', 'inbound', 1.0, 'ctwa_candidate', 1.0)",
            (event_id,),
        )

    @staticmethod
    def _record(
        ad_id: str = "120200000000001",
        *,
        name: str = "Lead ad",
        fetched_at: float = 100.0,
    ) -> MetaAdRecord:
        return MetaAdRecord(
            account_id=ACCOUNT_ID,
            ad_id=ad_id,
            ad_name=name,
            ad_status="ACTIVE",
            ad_effective_status="ACTIVE",
            adset_id="1203001",
            adset_name="Prospecting",
            adset_status="ACTIVE",
            campaign_id="1204001",
            campaign_name="September",
            campaign_status="ACTIVE",
            creative_id="1205001",
            creative_name="Image A",
            metadata_complete=True,
            fetched_at=fetched_at,
        )

    def write(self, callback):  # type: ignore[no-untyped-def]
        return self.runtime.write(callback)

    def test_staged_event_is_pending_then_exact_record_confirms_it(self) -> None:
        """Dropping the pending-to-confirmed transition would hide a resolvable lead."""
        self.write(lambda conn: self._event(conn, "waevt_one"))

        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        pending = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_one")
        )
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.status, "pending")
        self.assertTrue(
            self.write(
                lambda conn: self.store.claim_job(
                    conn, self.observed.source_id, now=101.0, lease_seconds=30.0
                )
            )
        )

        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn, self._record(), confirmed_at=102.0
            )
        )

        confirmed = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_one")
        )
        self.assertIsNotNone(confirmed)
        assert confirmed is not None
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(confirmed.record.ad_id, self.observed.source_id)
        self.assertEqual(confirmed.confirmed_at, 102.0)

    def test_duplicate_staging_keeps_one_job_but_rejects_changed_source(self) -> None:
        """A replay must not create duplicate work or silently rebind an event."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=101.0
            )
        )
        job_count = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM meta_attribution_jobs"
            ).fetchone()[0]
        )
        self.assertEqual(job_count, 1)
        with self.assertRaises(ValueError):
            self.write(
                lambda conn: self.store.stage_event(
                    conn,
                    "waevt_one",
                    ObservedAttribution("120200000000002", None),
                    now=102.0,
                )
            )

    def test_exact_resolution_confirms_all_events_sharing_one_job(self) -> None:
        """A shared source ID must be resolved once and applied to every pending event."""
        self.write(
            lambda conn: (
                self._event(conn, "waevt_one"),
                self._event(conn, "waevt_two"),
            )
        )
        self.write(
            lambda conn: (
                self.store.stage_event(conn, "waevt_one", self.observed, now=100.0),
                self.store.stage_event(conn, "waevt_two", self.observed, now=100.0),
            )
        )

        job_count = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM meta_attribution_jobs"
            ).fetchone()[0]
        )
        self.assertEqual(job_count, 1)
        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn, self._record(), confirmed_at=102.0
            )
        )
        statuses = self.runtime.read(
            lambda conn: tuple(
                row[0]
                for row in conn.execute(
                    "SELECT status FROM ctwa_meta_attributions ORDER BY event_id"
                )
            )
        )
        self.assertEqual(statuses, ("confirmed", "confirmed"))

    def test_job_lease_excludes_second_claim_until_expiry(self) -> None:
        """A live lookup must not run twice while its durable lease is active."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )

        first = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=101.0, lease_seconds=30.0
            )
        )
        excluded = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=102.0, lease_seconds=30.0
            )
        )
        recovered = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=131.0, lease_seconds=30.0
            )
        )
        self.assertEqual((first, excluded, recovered), (True, False, True))

    def test_failure_uses_bounded_retry_and_auth_circuit(self) -> None:
        """A failed Meta call must remain pending but cannot retry in a tight loop."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=100.0, lease_seconds=30.0
            )
        )
        self.write(
            lambda conn: self.store.fail_job(
                conn, self.observed.source_id, now=100.0, error_code="meta_not_found"
            )
        )
        next_attempt, errors = self.runtime.read(
            lambda conn: (
                conn.execute(
                    "SELECT next_attempt_at FROM meta_attribution_jobs"
                ).fetchone()[0],
                conn.execute(
                    "SELECT last_error_code FROM ctwa_meta_attributions"
                ).fetchone()[0],
            )
        )
        self.assertEqual(next_attempt, 160.0)
        self.assertEqual(errors, "meta_not_found")
        self.assertFalse(
            self.write(
                lambda conn: self.store.claim_job(
                    conn, self.observed.source_id, now=159.0, lease_seconds=30.0
                )
            )
        )
        self.assertTrue(
            self.write(
                lambda conn: self.store.claim_job(
                    conn, self.observed.source_id, now=160.0, lease_seconds=30.0
                )
            )
        )
        self.write(
            lambda conn: self.store.fail_job(
                conn,
                self.observed.source_id,
                now=160.0,
                error_code="meta_auth_unavailable",
            )
        )
        circuit_until = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT next_attempt_at FROM meta_attribution_jobs"
            ).fetchone()[0]
        )
        self.assertEqual(circuit_until, 3760.0)

    def test_catalog_refresh_updates_metadata_without_rebinding_confirmation(
        self,
    ) -> None:
        """Catalog refresh may change names, never the exact event-to-ad binding."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn,
                self._record(name="Original", fetched_at=100.0),
                confirmed_at=101.0,
            )
        )
        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn,
                self._record(name="Refreshed", fetched_at=200.0),
                confirmed_at=200.0,
            )
        )

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_one")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.record.ad_name, "Refreshed")
        self.assertEqual(view.record.ad_id, self.observed.source_id)
        self.assertEqual(view.confirmed_at, 101.0)

    def test_transaction_rollback_and_event_cascade_remove_attribution(self) -> None:
        """A crash rollback cannot leave work without its event, and retention cascades it."""

        def rollback(conn: sqlite3.Connection) -> None:
            self._event(conn, "waevt_rolled_back")
            self.store.stage_event(conn, "waevt_rolled_back", self.observed, now=100.0)
            raise RuntimeError("rollback")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            self.write(rollback)
        absent = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM ctwa_meta_attributions"
            ).fetchone()[0]
        )
        self.assertEqual(absent, 0)

        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        self.write(
            lambda conn: conn.execute(
                "DELETE FROM transport_events WHERE event_id = 'waevt_one'"
            )
        )
        attribution_count = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM ctwa_meta_attributions"
            ).fetchone()[0]
        )
        self.assertEqual(attribution_count, 0)

    def test_catalog_gc_keeps_recent_and_referenced_records_for_ninety_days(
        self,
    ) -> None:
        """GC must never erase a retained lead's proof or a record younger than 90 days."""
        old = 10_000_000.0
        ninety_days = 90 * 86_400
        self.write(lambda conn: self._event(conn, "waevt_referenced"))
        self.write(
            lambda conn: self.store.stage_event(
                conn,
                "waevt_referenced",
                ObservedAttribution("120200000000001", None),
                now=old,
            )
        )
        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn, self._record(fetched_at=old), confirmed_at=old
            )
        )
        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn,
                self._record("120200000000002", name="Unreferenced", fetched_at=old),
                confirmed_at=old,
            )
        )
        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn,
                self._record(
                    "120200000000003",
                    name="Recent",
                    fetched_at=old + ninety_days + 1,
                ),
                confirmed_at=old + ninety_days + 1,
            )
        )

        removed_early = self.write(
            lambda conn: self.store.purge_catalog(conn, now=old + ninety_days)
        )
        removed_late = self.write(
            lambda conn: self.store.purge_catalog(conn, now=old + ninety_days + 2)
        )
        ids = self.runtime.read(
            lambda conn: tuple(
                row[0]
                for row in conn.execute(
                    "SELECT ad_id FROM meta_ads_catalog ORDER BY ad_id"
                )
            )
        )
        self.assertEqual((removed_early, removed_late), (0, 1))
        self.assertEqual(ids, ("120200000000001", "120200000000003"))


if __name__ == "__main__":
    unittest.main()
