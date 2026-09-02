from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.meta_ads_models import (
    META_ERROR_CODES,
    MetaAdRecord,
    MetaAdsCapabilities,
    MetaAdsError,
)
from brain.meta_ads_store import MetaAdsStore
from brain.meta_attribution import MetaAttributionService
from brain.runtime_db import RuntimeDatabase

ACCOUNT_ID = "1598606388477916"
SOURCE_ID = "120200000000001"


class _FakeMetaAdsClient:
    def __init__(self, record: MetaAdRecord | None) -> None:
        self.record = record
        self.calls: list[tuple[str, str]] = []
        self.get_timeouts: list[float | None] = []
        self.get_error: MetaAdsError | None = None
        self.probe_error: MetaAdsError | None = None
        self.list_error: MetaAdsError | None = None
        self.list_records: list[MetaAdRecord] | None = None

    def get_ad(
        self,
        source_id: str,
        now: float,
        *,
        timeout_seconds: float | None = None,
    ) -> MetaAdRecord | None:
        self.calls.append(("get_ad", source_id))
        self.get_timeouts.append(timeout_seconds)
        if self.get_error is not None:
            raise self.get_error
        return self.record

    def list_ads(self, now: float, full: bool) -> list[MetaAdRecord]:
        self.calls.append(("list_ads", "full" if full else "incremental"))
        if self.list_error is not None:
            raise self.list_error
        if self.list_records is not None:
            return self.list_records
        return [] if self.record is None else [self.record]

    def probe(self) -> MetaAdsCapabilities:
        self.calls.append(("probe", ""))
        if self.probe_error is not None:
            raise self.probe_error
        return MetaAdsCapabilities(
            account_id=ACCOUNT_ID,
            required_tools=frozenset({"ads_get_ad_accounts", "ads_get_ad_entities"}),
            account_argument="ad_account_id",
            entity_selector_argument="entity_type",
            result_array_path=("data",),
            exact_id_filter_supported=True,
        )


class MetaAttributionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=0.25
        )
        self.runtime.initialize()
        self.store = MetaAdsStore(ACCOUNT_ID)
        self.record = MetaAdRecord(
            account_id=ACCOUNT_ID,
            ad_id=SOURCE_ID,
            ad_name="Lead ad",
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
            fetched_at=101.0,
        )
        self.client = _FakeMetaAdsClient(self.record)
        self.service = MetaAttributionService(
            self._settings(), self.runtime, self.store, self.client
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _settings(**overrides: object) -> BrainSettings:
        values: dict[str, object] = {
            "principals": {
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-token"),
                    frozenset({"conversation_context"}),
                )
            },
            "cursor_secret": b"c" * 32,
            "meta_attribution_enabled": True,
            "meta_ad_account_id": f"act_{ACCOUNT_ID}",
            "meta_ads_mcp_access_token": "fixture-token",
        }
        values.update(overrides)
        return BrainSettings(**values)

    @staticmethod
    def _event(conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "INSERT INTO transport_events "
            "(event_id, observer_device_id, direction, received_at, transport_kind, "
            "created_at) VALUES (?, 'observer-a', 'inbound', 1.0, 'ctwa_candidate', 1.0)",
            (event_id,),
        )

    def test_live_exact_resolution_confirms_pending_event(self) -> None:
        """Dropping exact proof would leave a resolvable lead permanently pending."""
        raw_ctwa = {"sourceType": "ad", "sourceId": SOURCE_ID, "ctwaClid": "clid"}
        self.runtime.write(lambda conn: self._event(conn, "waevt_one"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn, event_id="waevt_one", raw=raw_ctwa, now=100.0
            )
        )

        self.assertTrue(self.service.resolve_source(SOURCE_ID, now=101.0))

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_one")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, "confirmed")
        self.assertEqual(view.record.ad_id, SOURCE_ID)
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def test_context_resolution_passes_the_remaining_budget_to_the_meta_client(
        self,
    ) -> None:
        """Ignoring the context budget could make the CEO wait past its deadline."""
        self.runtime.write(lambda conn: self._event(conn, "waevt_context_budget"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_context_budget",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        self.assertTrue(
            self.service.resolve_contact_pending(
                "waevt_context_budget", now=101.0, budget_seconds=0.25
            )
        )

        self.assertEqual(self.client.get_timeouts, [0.25])

    def test_disabled_service_never_stages_or_calls_meta(self) -> None:
        """Enabling attribution by accident must be required before any Meta work exists."""
        disabled = MetaAttributionService(
            self._settings(
                meta_attribution_enabled=False,
                meta_ad_account_id="",
                meta_ads_mcp_access_token="",
            ),
            self.runtime,
            self.store,
            self.client,
        )
        self.runtime.write(lambda conn: self._event(conn, "waevt_disabled"))

        staged = self.runtime.write(
            lambda conn: disabled.stage_event(
                conn,
                event_id="waevt_disabled",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        self.assertIsNone(staged)
        self.assertFalse(disabled.resolve_source(SOURCE_ID, now=101.0))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(disabled.health(101.0).status, "disabled")

    def test_no_exact_meta_record_stays_pending_with_not_found_retry(self) -> None:
        """Treating an empty exact lookup as a match would fabricate attribution."""
        client = _FakeMetaAdsClient(None)
        service = MetaAttributionService(
            self._settings(), self.runtime, self.store, client
        )
        self.runtime.write(lambda conn: self._event(conn, "waevt_missing"))
        self.runtime.write(
            lambda conn: service.stage_event(
                conn,
                event_id="waevt_missing",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        self.assertFalse(service.resolve_source(SOURCE_ID, now=101.0))

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_missing")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, "pending")
        self.assertEqual(view.last_error_code, "meta_not_found")

    def test_catalog_hit_confirms_new_event_without_meta_request(self) -> None:
        """A validated durable catalog proof must avoid an unnecessary live request."""
        self.runtime.write(
            lambda conn: self.store.upsert_record_and_confirm(
                conn, self.record, confirmed_at=99.0
            )
        )
        self.runtime.write(lambda conn: self._event(conn, "waevt_cached"))

        view = self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_cached",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, "confirmed")
        self.assertEqual(self.client.calls, [])

    def test_one_live_lookup_confirms_all_events_with_the_same_source(self) -> None:
        """A duplicate source ID must share one exact lookup and one durable outcome."""
        self.runtime.write(
            lambda conn: (
                self._event(conn, "waevt_first"),
                self._event(conn, "waevt_second"),
            )
        )
        self.runtime.write(
            lambda conn: (
                self.service.stage_event(
                    conn,
                    event_id="waevt_first",
                    raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                    now=100.0,
                ),
                self.service.stage_event(
                    conn,
                    event_id="waevt_second",
                    raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                    now=100.0,
                ),
            )
        )

        self.assertTrue(self.service.resolve_source(SOURCE_ID, now=101.0))

        statuses = self.runtime.read(
            lambda conn: tuple(
                row[0]
                for row in conn.execute(
                    "SELECT status FROM ctwa_meta_attributions ORDER BY event_id"
                )
            )
        )
        self.assertEqual(statuses, ("confirmed", "confirmed"))
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def test_auth_failure_opens_circuit_until_a_successful_probe(self) -> None:
        """Repeated bad credentials must not trigger repeated live lookups."""
        self.client.get_error = MetaAdsError("meta_auth_unavailable")
        self.runtime.write(
            lambda conn: (
                self._event(conn, "waevt_auth_one"),
                self._event(conn, "waevt_auth_two"),
            )
        )
        self.runtime.write(
            lambda conn: (
                self.service.stage_event(
                    conn,
                    event_id="waevt_auth_one",
                    raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                    now=100.0,
                ),
                self.service.stage_event(
                    conn,
                    event_id="waevt_auth_two",
                    raw={"sourceType": "ad", "sourceId": "120200000000002"},
                    now=100.0,
                ),
            )
        )

        self.assertFalse(self.service.resolve_source(SOURCE_ID, now=100.0))
        self.assertFalse(self.service.resolve_source("120200000000002", now=101.0))
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])
        self.assertEqual(self.service.health(101.0).status, "degraded")

        self.client.get_error = None
        self.assertIsNotNone(self.service.probe(now=3701.0))
        self.assertEqual(self.service.health(3701.0).status, "ready")

    def test_refresh_updates_metadata_without_rebinding_the_confirmed_ad(self) -> None:
        """Refreshing names must preserve the original event's exact ad identity."""
        self.runtime.write(lambda conn: self._event(conn, "waevt_refresh"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_refresh",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.assertTrue(self.service.resolve_source(SOURCE_ID, now=101.0))
        refreshed = MetaAdRecord(
            **{**self.record.__dict__, "ad_name": "Renamed ad", "fetched_at": 200.0}
        )
        self.client.list_records = [refreshed]

        self.assertEqual(self.service.refresh_catalog(200.0, full=True), 0)

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_refresh")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.record.ad_id, SOURCE_ID)
        self.assertEqual(view.record.ad_name, "Renamed ad")
        self.assertGreaterEqual(view.confirmed_at, 101.0)
        self.assertLess(view.confirmed_at, 102.0)

    def test_concurrent_resolvers_make_one_network_call_for_one_lease(self) -> None:
        """Removing the durable claim would duplicate a remote lookup under contention."""
        self.runtime.write(lambda conn: self._event(conn, "waevt_concurrent"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_concurrent",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        started = threading.Event()
        release = threading.Event()

        def delayed_lookup(source_id: str, now: float) -> MetaAdRecord:
            self.client.calls.append(("get_ad", source_id))
            started.set()
            self.assertTrue(release.wait(timeout=2.0))
            return self.record

        self.client.get_ad = delayed_lookup  # type: ignore[method-assign]
        results: list[bool] = []

        first = threading.Thread(
            target=lambda: results.append(self.service.resolve_source(SOURCE_ID, 101.0))
        )
        first.start()
        self.assertTrue(started.wait(timeout=2.0))
        second = threading.Thread(
            target=lambda: results.append(self.service.resolve_source(SOURCE_ID, 101.0))
        )
        second.start()
        second.join(timeout=2.0)
        release.set()
        first.join(timeout=2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def test_late_lookup_loses_the_expired_lease_without_confirmation(self) -> None:
        """A response after the lease expiry must not defeat a newer resolver."""
        self.runtime.write(lambda conn: self._event(conn, "waevt_late"))
        clock_values = iter((0.0, 31.0))
        service = MetaAttributionService(
            self._settings(),
            self.runtime,
            self.store,
            self.client,
            monotonic_clock=lambda: next(clock_values),
        )
        self.runtime.write(
            lambda conn: service.stage_event(
                conn,
                event_id="waevt_late",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        def late_lookup(source_id: str, now: float) -> MetaAdRecord:
            self.client.calls.append(("get_ad", source_id))
            replacement = self.runtime.write(
                lambda conn: self.store.claim_job(
                    conn, source_id, now=131.0, lease_seconds=30.0
                )
            )
            self.assertIsInstance(replacement, str)
            return self.record

        self.client.get_ad = late_lookup  # type: ignore[method-assign]

        self.assertFalse(service.resolve_source(SOURCE_ID, now=100.0))

        state = self.runtime.read(
            lambda conn: (
                self.store.context_for_event(conn, "waevt_late").status,
                conn.execute("SELECT COUNT(*) FROM meta_ads_catalog").fetchone()[0],
                conn.execute(
                    "SELECT lease_until FROM meta_attribution_jobs WHERE source_id = ?",
                    (SOURCE_ID,),
                ).fetchone()[0],
            )
        )
        self.assertEqual(state, ("pending", 0, 161.0))

    def test_restart_preserves_auth_circuit_for_pending_jobs(self) -> None:
        """Restarting during auth backoff must not immediately call Meta again."""
        self.client.get_error = MetaAdsError("meta_auth_unavailable")
        self.runtime.write(lambda conn: self._event(conn, "waevt_circuit_restart"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_circuit_restart",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.assertFalse(self.service.resolve_source(SOURCE_ID, now=100.0))
        restarted = MetaAttributionService(
            self._settings(), self.runtime, self.store, self.client
        )

        self.assertEqual(restarted.run_due_jobs(now=101.0), 0)

        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def _restart_during_auth_circuit(self, event_id: str) -> MetaAttributionService:
        self.client.get_error = MetaAdsError("meta_auth_unavailable")
        self.runtime.write(lambda conn: self._event(conn, event_id))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.assertFalse(self.service.resolve_source(SOURCE_ID, now=100.0))
        self.client.get_error = None
        return MetaAttributionService(
            self._settings(), self.runtime, self.store, self.client
        )

    def test_restarted_probe_does_not_call_meta_during_durable_auth_circuit(
        self,
    ) -> None:
        """A fresh process must honor auth deferral before capability discovery."""
        restarted = self._restart_during_auth_circuit("waevt_probe_circuit")

        self.assertIsNone(restarted.probe(now=101.0))

        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def test_restarted_refresh_does_not_call_meta_during_durable_auth_circuit(
        self,
    ) -> None:
        """A fresh process must honor auth deferral before catalog synchronization."""
        restarted = self._restart_during_auth_circuit("waevt_refresh_circuit")

        self.assertEqual(restarted.refresh_catalog(now=101.0), 0)

        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def test_restarted_tick_skips_all_meta_operations_during_durable_auth_circuit(
        self,
    ) -> None:
        """The worker tick must not bypass a persistent auth circuit after restart."""
        restarted = self._restart_during_auth_circuit("waevt_tick_circuit")

        self.assertEqual(restarted.tick(now=101.0), 0)

        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

    def test_failed_probe_without_jobs_defers_new_work_after_restart(self) -> None:
        """An account auth failure must survive even when there were no jobs to defer."""
        self.client.probe_error = MetaAdsError("meta_auth_unavailable")

        self.assertIsNone(self.service.probe(now=100.0))
        self.assertEqual(self.client.calls, [("probe", "")])

        restarted = MetaAttributionService(
            self._settings(), self.runtime, self.store, self.client
        )
        self.runtime.write(lambda conn: self._event(conn, "waevt_probe_zero_jobs"))
        self.runtime.write(
            lambda conn: restarted.stage_event(
                conn,
                event_id="waevt_probe_zero_jobs",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=101.0,
            )
        )

        self.assertEqual(restarted.tick(now=101.0), 0)

        state = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT auth_circuit_until FROM meta_attribution_state WHERE account_id = ?",
                (ACCOUNT_ID,),
            ).fetchone()[0]
        )
        self.assertGreaterEqual(state, 3700.0)
        self.assertEqual(self.client.calls, [("probe", "")])

    def test_successful_probe_closes_the_durable_auth_circuit(self) -> None:
        """A rotated credential must release pending jobs, even for a later worker."""
        self.client.get_error = MetaAdsError("meta_auth_unavailable")
        self.runtime.write(lambda conn: self._event(conn, "waevt_circuit_closed"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_circuit_closed",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.assertFalse(self.service.resolve_source(SOURCE_ID, now=100.0))
        self.client.get_error = None

        self.assertIsNotNone(self.service.probe(now=3701.0))
        restarted = MetaAttributionService(
            self._settings(), self.runtime, self.store, self.client
        )

        self.assertEqual(restarted.run_due_jobs(now=3701.0), 1)

    def test_rotated_credential_probe_closes_auth_circuit_before_expiry(
        self,
    ) -> None:
        """Dropping credential identity would keep a validated replacement blocked."""
        self.client.get_error = MetaAdsError("meta_auth_unavailable")
        self.runtime.write(lambda conn: self._event(conn, "waevt_rotated_credential"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_rotated_credential",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.assertFalse(self.service.resolve_source(SOURCE_ID, now=100.0))
        self.client.get_error = None

        same_credential_runtime = RuntimeDatabase(
            self.runtime.path, timeout_seconds=0.25
        )
        same_credential_runtime.initialize()
        same_credential = MetaAttributionService(
            self._settings(cursor_secret=b"d" * 32),
            same_credential_runtime,
            MetaAdsStore(ACCOUNT_ID),
            self.client,
        )
        self.assertIsNone(same_credential.probe(now=101.0))
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID)])

        rotated_runtime = RuntimeDatabase(self.runtime.path, timeout_seconds=0.25)
        rotated_runtime.initialize()
        rotated = MetaAttributionService(
            self._settings(meta_ads_mcp_access_token="rotated-fixture-token"),
            rotated_runtime,
            MetaAdsStore(ACCOUNT_ID),
            self.client,
        )

        self.assertIsNotNone(rotated.probe(now=101.0))

        circuit_state = rotated_runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT auth_circuit_until, auth_credential_fingerprint "
                    "FROM meta_attribution_state WHERE account_id = ?",
                    (ACCOUNT_ID,),
                ).fetchone()
            )
        )
        self.assertEqual(circuit_state, (101.0, None))
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID), ("probe", "")])

        confirmed_runtime = RuntimeDatabase(self.runtime.path, timeout_seconds=0.25)
        confirmed_runtime.initialize()
        confirmed = MetaAttributionService(
            self._settings(meta_ads_mcp_access_token="rotated-fixture-token"),
            confirmed_runtime,
            MetaAdsStore(ACCOUNT_ID),
            self.client,
        )

        self.assertEqual(confirmed.run_due_jobs(now=101.0), 1)
        view = confirmed_runtime.read(
            lambda conn: MetaAdsStore(ACCOUNT_ID).context_for_event(
                conn, "waevt_rotated_credential"
            )
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, "confirmed")

    def test_unsuccessful_rotated_credential_probe_keeps_auth_circuit_open(
        self,
    ) -> None:
        """Closing on a failed probe would let a bad replacement retry through jobs."""
        self.client.get_error = MetaAdsError("meta_auth_unavailable")
        self.runtime.write(lambda conn: self._event(conn, "waevt_failed_rotation"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_failed_rotation",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.assertFalse(self.service.resolve_source(SOURCE_ID, now=100.0))
        self.client.get_error = None
        self.client.probe_error = MetaAdsError("meta_server_unavailable")

        rotated_runtime = RuntimeDatabase(self.runtime.path, timeout_seconds=0.25)
        rotated_runtime.initialize()
        rotated = MetaAttributionService(
            self._settings(meta_ads_mcp_access_token="rotated-fixture-token"),
            rotated_runtime,
            MetaAdsStore(ACCOUNT_ID),
            self.client,
        )

        self.assertIsNone(rotated.probe(now=101.0))

        after_failed_probe = MetaAttributionService(
            self._settings(meta_ads_mcp_access_token="rotated-fixture-token"),
            RuntimeDatabase(self.runtime.path, timeout_seconds=0.25),
            MetaAdsStore(ACCOUNT_ID),
            self.client,
        )
        self.assertEqual(after_failed_probe.run_due_jobs(now=101.0), 0)
        self.assertEqual(self.client.calls, [("get_ad", SOURCE_ID), ("probe", "")])

    def test_restart_recovers_an_expired_lease_and_runs_the_due_job(self) -> None:
        """A process restart must not strand attribution behind an old worker lease."""
        self.runtime.write(lambda conn: self._event(conn, "waevt_restart"))
        self.runtime.write(
            lambda conn: self.service.stage_event(
                conn,
                event_id="waevt_restart",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )
        self.runtime.write(
            lambda conn: self.store.claim_job(
                conn, SOURCE_ID, now=100.0, lease_seconds=30.0
            )
        )
        restarted = MetaAttributionService(
            self._settings(), self.runtime, self.store, self.client
        )

        self.assertEqual(restarted.run_due_jobs(now=131.0), 1)

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_restart")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, "confirmed")

    def test_health_reports_expiring_credential_without_attribution_values(
        self,
    ) -> None:
        """Health must warn about credential lifecycle without exposing a lead or ad."""
        for days in (14, 7, 3):
            with self.subTest(days=days):
                expiring = MetaAttributionService(
                    self._settings(meta_ads_mcp_token_expires_at=100.0 + days * 86_400),
                    self.runtime,
                    self.store,
                    self.client,
                )

                health = expiring.health(100.0)

                self.assertEqual(health.status, "degraded")
                self.assertEqual(health.credential_status, "expiring")
                self.assertNotIn(SOURCE_ID, repr(health))
                self.assertNotIn("Lead ad", repr(health))

    def test_missing_credential_reschedules_without_a_meta_call(self) -> None:
        """A known missing token must leave the lead pending without calling Meta."""
        missing_token = MetaAttributionService(
            self._settings(meta_ads_mcp_access_token=""),
            self.runtime,
            self.store,
            self.client,
        )
        self.runtime.write(lambda conn: self._event(conn, "waevt_missing_token"))
        self.runtime.write(
            lambda conn: missing_token.stage_event(
                conn,
                event_id="waevt_missing_token",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        self.assertFalse(missing_token.resolve_source(SOURCE_ID, now=100.0))

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_missing_token")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.last_error_code, "meta_auth_unavailable")
        self.assertEqual(self.client.calls, [])

    def test_every_bounded_meta_error_keeps_the_event_pending(self) -> None:
        """Changing any remote error into confirmation would invent lead provenance."""
        error_codes = sorted(META_ERROR_CODES - {"meta_auth_unavailable"})
        error_codes.append("meta_auth_unavailable")
        for index, code in enumerate(error_codes, start=10):
            with self.subTest(code=code):
                source_id = f"1202000000000{index:02d}"
                client = _FakeMetaAdsClient(self.record)
                client.get_error = MetaAdsError(
                    code,
                    retry_after_seconds=600.0 if code == "meta_rate_limited" else None,
                )
                service = MetaAttributionService(
                    self._settings(), self.runtime, self.store, client
                )
                event_id = f"waevt_error_{index}"
                self.runtime.write(
                    lambda conn, event_id=event_id: self._event(conn, event_id)
                )
                self.runtime.write(
                    lambda conn, event_id=event_id, source_id=source_id, service=service: (
                        service.stage_event(
                            conn,
                            event_id=event_id,
                            raw={"sourceType": "ad", "sourceId": source_id},
                            now=100.0,
                        )
                    )
                )

                self.assertFalse(service.resolve_source(source_id, now=100.0))

                view = self.runtime.read(
                    lambda conn, event_id=event_id: self.store.context_for_event(
                        conn, event_id
                    )
                )
                self.assertIsNotNone(view)
                assert view is not None
                self.assertEqual(view.status, "pending")
                self.assertEqual(view.last_error_code, code)
                if code == "meta_rate_limited":
                    scheduled = self.runtime.read(
                        lambda conn, source_id=source_id: conn.execute(
                            "SELECT next_attempt_at FROM meta_attribution_jobs "
                            "WHERE account_id = ? AND source_id = ?",
                            (ACCOUNT_ID, source_id),
                        ).fetchone()[0]
                    )
                    self.assertGreaterEqual(scheduled, 700.0)
                    self.assertLess(scheduled, 701.0)

    def test_wrong_ad_id_from_client_never_confirms_a_source(self) -> None:
        """A client regression that returns a neighbouring ad ID must remain pending."""
        wrong_record = MetaAdRecord(
            **{**self.record.__dict__, "ad_id": "120200000000099"}
        )
        service = MetaAttributionService(
            self._settings(), self.runtime, self.store, _FakeMetaAdsClient(wrong_record)
        )
        self.runtime.write(lambda conn: self._event(conn, "waevt_wrong_ad"))
        self.runtime.write(
            lambda conn: service.stage_event(
                conn,
                event_id="waevt_wrong_ad",
                raw={"sourceType": "ad", "sourceId": SOURCE_ID},
                now=100.0,
            )
        )

        self.assertFalse(service.resolve_source(SOURCE_ID, now=101.0))

        view = self.runtime.read(
            lambda conn: self.store.context_for_event(conn, "waevt_wrong_ad")
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, "pending")
        self.assertEqual(view.last_error_code, "meta_invalid_response")

    def test_tick_with_missing_credential_does_not_call_catalog_tools(self) -> None:
        """The worker must not hammer Meta when local credential state is already bad."""
        missing_token = MetaAttributionService(
            self._settings(meta_ads_mcp_access_token=""),
            self.runtime,
            self.store,
            self.client,
        )

        self.assertEqual(missing_token.tick(now=100.0), 0)

        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
