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
        lease_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=101.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(lease_token, str)

        self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn,
                self._record(),
                confirmed_at=102.0,
                lease_token=lease_token,
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
        self.assertIsInstance(first, str)
        self.assertIsNone(excluded)
        self.assertIsInstance(recovered, str)

    def test_stale_lease_cannot_confirm_or_delete_the_new_owner_job(self) -> None:
        """A late lookup must not erase a newer resolver's durable lease."""
        self.write(lambda conn: self._event(conn, "waevt_stale"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_stale", self.observed, now=100.0
            )
        )
        stale_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=100.0, lease_seconds=30.0
            )
        )
        current_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=131.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(stale_token, str)
        self.assertIsInstance(current_token, str)

        confirmed = self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn,
                self._record(fetched_at=131.0),
                confirmed_at=131.0,
                lease_token=stale_token,
            )
        )

        self.assertEqual(confirmed, 0)
        state = self.runtime.read(
            lambda conn: (
                conn.execute("SELECT COUNT(*) FROM meta_ads_catalog").fetchone()[0],
                tuple(
                    conn.execute(
                        "SELECT lease_token, lease_until FROM meta_attribution_jobs"
                    ).fetchone()
                ),
                self.store.context_for_event(conn, "waevt_stale").status,
            )
        )
        self.assertEqual(state, (0, (current_token, 161.0), "pending"))

    def test_unleased_catalog_refresh_cannot_complete_an_active_claim(self) -> None:
        """A refresh must not steal a live resolver's lease or confirmation."""
        self.write(lambda conn: self._event(conn, "waevt_refresh_claim"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_refresh_claim", self.observed, now=100.0
            )
        )
        lease_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=100.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(lease_token, str)

        refreshed = self.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn, self._record(), confirmed_at=101.0
            )
        )

        def state(conn: sqlite3.Connection) -> tuple[str, tuple[object, ...] | None]:
            job = conn.execute(
                "SELECT lease_token, lease_until FROM meta_attribution_jobs"
            ).fetchone()
            return (
                self.store.context_for_event(conn, "waevt_refresh_claim").status,
                None if job is None else tuple(job),
            )

        state = self.runtime.read(state)
        self.assertEqual(refreshed, 0)
        self.assertEqual(state, ("pending", (lease_token, 130.0)))

    def test_auth_failure_defers_existing_and_new_jobs_across_a_restart(self) -> None:
        """A credential circuit must survive workers and cover work staged later."""
        first = ObservedAttribution("120200000000001", None)
        second = ObservedAttribution("120200000000002", None)
        third = ObservedAttribution("120200000000003", None)
        self.write(
            lambda conn: (
                self._event(conn, "waevt_auth_first"),
                self._event(conn, "waevt_auth_second"),
            )
        )
        self.write(
            lambda conn: (
                self.store.stage_event(conn, "waevt_auth_first", first, now=100.0),
                self.store.stage_event(conn, "waevt_auth_second", second, now=100.0),
            )
        )
        lease_token = self.write(
            lambda conn: self.store.claim_job(
                conn, first.source_id, now=100.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(lease_token, str)
        self.assertTrue(
            self.write(
                lambda conn: self.store.fail_job(
                    conn,
                    first.source_id,
                    now=100.0,
                    error_code="meta_auth_unavailable",
                    lease_token=lease_token,
                )
            )
        )
        self.write(lambda conn: self._event(conn, "waevt_auth_third"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_auth_third", third, now=101.0
            )
        )

        jobs = self.runtime.read(
            lambda conn: tuple(
                tuple(row)
                for row in conn.execute(
                    "SELECT source_id, next_attempt_at, lease_token, lease_until, "
                    "last_error_code FROM meta_attribution_jobs ORDER BY source_id"
                )
            )
        )
        self.assertEqual(
            jobs,
            (
                (first.source_id, 3700.0, None, None, "meta_auth_unavailable"),
                (second.source_id, 3700.0, None, None, "meta_auth_unavailable"),
                (third.source_id, 3700.0, None, None, None),
            ),
        )
        restarted = MetaAdsStore(ACCOUNT_ID)
        self.assertEqual(
            self.runtime.read(lambda conn: restarted.due_source_ids(conn, now=3699.0)),
            (),
        )

    def test_failure_uses_bounded_retry_and_auth_circuit(self) -> None:
        """A failed Meta call must remain pending but cannot retry in a tight loop."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        self.assertFalse(
            self.write(
                lambda conn: self.store.fail_job(
                    conn,
                    self.observed.source_id,
                    now=100.0,
                    error_code="meta_timeout",
                    lease_token="no-current-owner",
                )
            )
        )
        first_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=100.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(first_token, str)
        self.write(
            lambda conn: self.store.fail_job(
                conn,
                self.observed.source_id,
                now=100.0,
                error_code="meta_not_found",
                lease_token=first_token,
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
        second_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=160.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(second_token, str)
        self.write(
            lambda conn: self.store.fail_job(
                conn,
                self.observed.source_id,
                now=160.0,
                error_code="meta_auth_unavailable",
                lease_token=second_token,
            )
        )
        circuit_until = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT next_attempt_at FROM meta_attribution_jobs"
            ).fetchone()[0]
        )
        self.assertEqual(circuit_until, 3760.0)

    def test_retry_after_larger_than_normal_cap_remains_the_next_attempt(self) -> None:
        """An upstream Retry-After must outrank local backoff capping."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        lease_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=100.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(lease_token, str)

        self.assertTrue(
            self.write(
                lambda conn: self.store.fail_job(
                    conn,
                    self.observed.source_id,
                    now=100.0,
                    error_code="meta_rate_limited",
                    retry_after_seconds=172_800.0,
                    lease_token=lease_token,
                )
            )
        )
        next_attempt = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT next_attempt_at FROM meta_attribution_jobs"
            ).fetchone()[0]
        )
        self.assertEqual(next_attempt, 172_900.0)

    def test_only_current_lease_owner_can_complete_a_failed_job(self) -> None:
        """A stale resolver must not clear a newer worker's lease or retry state."""
        self.write(lambda conn: self._event(conn, "waevt_one"))
        self.write(
            lambda conn: self.store.stage_event(
                conn, "waevt_one", self.observed, now=100.0
            )
        )
        first_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=100.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(first_token, str)
        current_token = self.write(
            lambda conn: self.store.claim_job(
                conn, self.observed.source_id, now=130.0, lease_seconds=30.0
            )
        )
        self.assertIsInstance(current_token, str)
        self.assertNotEqual(first_token, current_token)

        self.assertFalse(
            self.write(
                lambda conn: self.store.fail_job(
                    conn,
                    self.observed.source_id,
                    now=130.0,
                    error_code="meta_timeout",
                    lease_token="unknown-owner",
                )
            )
        )
        self.assertFalse(
            self.write(
                lambda conn: self.store.fail_job(
                    conn,
                    self.observed.source_id,
                    now=130.0,
                    error_code="meta_timeout",
                    lease_token=first_token,
                )
            )
        )
        state_before_current_owner = self.runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT attempt_count, lease_token, lease_until, last_error_code "
                    "FROM meta_attribution_jobs"
                ).fetchone()
            )
        )
        self.assertEqual(state_before_current_owner, (0, current_token, 160.0, None))

        self.assertTrue(
            self.write(
                lambda conn: self.store.fail_job(
                    conn,
                    self.observed.source_id,
                    now=130.0,
                    error_code="meta_timeout",
                    lease_token=current_token,
                )
            )
        )
        state_after_current_owner = self.runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT attempt_count, lease_token, lease_until, last_error_code "
                    "FROM meta_attribution_jobs"
                ).fetchone()
            )
        )
        self.assertEqual(state_after_current_owner, (1, None, None, "meta_timeout"))

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
