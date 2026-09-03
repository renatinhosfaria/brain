from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.meta_ads_models import MetaAdsError, RemoteAd, RemoteCampaign
from brain.meta_attribution import MetaAttributionService
from brain.runtime_db import RuntimeDatabase


class _Client:
    def __init__(
        self,
        *,
        ad: object | None = None,
        campaign: object | None = None,
        probe_error: MetaAdsError | None = None,
        ad_error: MetaAdsError | None = None,
        on_probe: Callable[[], None] | None = None,
    ) -> None:
        self.ad = ad or RemoteAd("101", "Ad", "202", "PAUSED", "ACTIVE")
        self.campaign = campaign or RemoteCampaign(
            "202", "Campaign", "ACTIVE", "ACTIVE"
        )
        self.probe_error = probe_error
        self.ad_error = ad_error
        self.on_probe = on_probe
        self.auth_latched = False
        self.calls: list[tuple[str, str | None, float | None]] = []

    def probe(self, deadline: float | None = None) -> None:
        self.calls.append(("probe", None, deadline))
        if self.on_probe is not None:
            self.on_probe()
        if self.probe_error:
            if self.probe_error.code == "meta_auth_unavailable":
                self.auth_latched = True
            raise self.probe_error
        if self.auth_latched:
            raise MetaAdsError("meta_auth_unavailable")

    def invalidate(self) -> None:
        self.auth_latched = False

    def get_ad(self, source_id: str, deadline: float | None = None) -> object:
        self.calls.append(("ad", source_id, deadline))
        if self.ad_error:
            raise self.ad_error
        return self.ad

    def get_campaign(self, campaign_id: str, deadline: float | None = None) -> object:
        self.calls.append(("campaign", campaign_id, deadline))
        return self.campaign


class _AdvancingClient(_Client):
    def __init__(self, clock: list[float]) -> None:
        super().__init__()
        self.clock = clock

    def probe(self, deadline: float | None = None) -> None:
        super().probe(deadline)
        self.clock[0] = (deadline or self.clock[0]) + 0.01


class MetaAttributionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=0.25
        )
        self.runtime.initialize()
        self.settings = BrainSettings(
            meta_ads_mcp_enabled=True,
            meta_ads_mcp_api_key="test-key",
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-token"),
                    frozenset({"conversation_context"}),
                )
            },
            cursor_secret=b"c" * 32,
        )
        self.client = _Client()
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def stage(self, event_id: str = "event-1", source_id: str = "101") -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO transport_events "
                "(event_id, observer_device_id, direction, received_at, transport_kind, created_at) "
                "VALUES (?, 'observer', 'inbound', 1, 'ctwa_candidate', 1)",
                (event_id,),
            )
            self.service.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": source_id, "ctwaClid": "clid"},
                now=10.0,
            )

        self.runtime.write(write)

    def row(self, event_id: str = "event-1") -> tuple[object, ...]:
        return self.runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT status, reason_code, ad_id, ad_name, campaign_id, campaign_name "
                    "FROM ctwa_meta_attributions WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            )
        )

    def test_stages_only_a_valid_original_ad_source_in_the_caller_transaction(
        self,
    ) -> None:
        def rollback(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO transport_events "
                "(event_id, observer_device_id, direction, received_at, transport_kind, created_at) "
                "VALUES ('event-1', 'observer', 'inbound', 1, 'ctwa_candidate', 1)"
            )
            self.service.stage_event(
                conn,
                event_id="event-1",
                raw={"sourceType": "ad", "sourceId": "101"},
                now=10.0,
            )
            raise RuntimeError("rollback")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            self.runtime.write(rollback)
        self.assertEqual(
            self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM ctwa_meta_attributions"
                ).fetchone()[0]
            ),
            0,
        )

    def test_confirms_only_the_exact_active_ad_and_campaign(self) -> None:
        self.stage()

        self.assertTrue(self.service.resolve_source("101", 20.0, 1.0))
        self.assertEqual(
            self.row(),
            ("confirmed", None, "101", "Ad", "202", "Campaign"),
        )
        self.assertEqual(
            [call[:2] for call in self.client.calls],
            [("probe", None), ("ad", "101"), ("campaign", "202")],
        )
        self.assertEqual(len({call[2] for call in self.client.calls}), 1)
        self.assertFalse(self.service.resolve_source("101", 21.0, 1.0))
        self.assertEqual(len(self.client.calls), 3)

    def test_absolute_deadline_prevents_ad_call_after_probe_consumes_budget(
        self,
    ) -> None:
        clock = [100.0]
        self.client = _AdvancingClient(clock)
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)
        self.stage()

        with patch(
            "brain.meta_attribution.time.monotonic", side_effect=lambda: clock[0]
        ):
            self.assertFalse(self.service.resolve_source("101", 20.0, deadline=101.0))

        self.assertEqual([call[:2] for call in self.client.calls], [("probe", None)])

    def test_absolute_deadline_prevents_probe_after_claim_consumes_budget(self) -> None:
        clock = [100.0]
        self.stage()
        original_write = self.runtime.write
        writes = 0

        def advancing_write(callback):
            nonlocal writes
            result = original_write(callback)
            writes += 1
            if writes == 1:
                clock[0] = 101.01
            return result

        with (
            patch.object(self.runtime, "write", side_effect=advancing_write),
            patch(
                "brain.meta_attribution.time.monotonic", side_effect=lambda: clock[0]
            ),
        ):
            self.assertFalse(self.service.resolve_source("101", 20.0, deadline=101.0))

        self.assertEqual(self.client.calls, [])

    def test_rejects_each_unconfirmed_ad_or_campaign_predicate_without_names(
        self,
    ) -> None:
        cases = (
            (
                "ad id",
                lambda source: _Client(
                    ad=RemoteAd("999", "Ad", "202", "ACTIVE", "ACTIVE")
                ),
                "meta_not_found",
            ),
            (
                "ad name",
                lambda source: _Client(
                    ad=SimpleNamespace(
                        ad_id=source,
                        name="",
                        campaign_id="202",
                        status="ACTIVE",
                        effective_status="ACTIVE",
                    )
                ),
                "meta_incomplete_result",
            ),
            (
                "ad status",
                lambda source: _Client(
                    ad=SimpleNamespace(
                        ad_id=source,
                        name="Ad",
                        campaign_id="202",
                        status="",
                        effective_status="ACTIVE",
                    )
                ),
                "meta_incomplete_result",
            ),
            (
                "ad active",
                lambda source: _Client(
                    ad=RemoteAd(source, "Ad", "202", "ACTIVE", "PAUSED")
                ),
                "meta_inactive",
            ),
            (
                "campaign id",
                lambda source: _Client(
                    ad=RemoteAd(source, "Ad", "202", "ACTIVE", "ACTIVE"),
                    campaign=RemoteCampaign("203", "Campaign", "ACTIVE", "ACTIVE"),
                ),
                "meta_not_found",
            ),
            (
                "campaign name",
                lambda source: _Client(
                    ad=RemoteAd(source, "Ad", "202", "ACTIVE", "ACTIVE"),
                    campaign=SimpleNamespace(
                        campaign_id="202",
                        name="",
                        status="ACTIVE",
                        effective_status="ACTIVE",
                    ),
                ),
                "meta_incomplete_result",
            ),
            (
                "campaign status",
                lambda source: _Client(
                    ad=RemoteAd(source, "Ad", "202", "ACTIVE", "ACTIVE"),
                    campaign=SimpleNamespace(
                        campaign_id="202",
                        name="Campaign",
                        status="",
                        effective_status="ACTIVE",
                    ),
                ),
                "meta_incomplete_result",
            ),
            (
                "campaign active",
                lambda source: _Client(
                    ad=RemoteAd(source, "Ad", "202", "ACTIVE", "ACTIVE"),
                    campaign=RemoteCampaign("202", "Campaign", "ACTIVE", "PAUSED"),
                ),
                "meta_inactive",
            ),
        )
        for index, (name, build_client, reason) in enumerate(cases):
            with self.subTest(name=name):
                source_id = str(101 + index)
                client = build_client(source_id)
                self.client = client
                self.service = MetaAttributionService(
                    self.settings, self.runtime, client
                )
                event_id = f"event-{name}"
                self.stage(event_id, source_id)
                self.assertFalse(self.service.resolve_source(source_id, 20.0, 1.0))
                status, stored_reason, ad_id, ad_name, campaign_id, campaign_name = (
                    self.row(event_id)
                )
                self.assertEqual(stored_reason, reason)
                self.assertIsNone(ad_id)
                self.assertIsNone(ad_name)
                self.assertIsNone(campaign_id)
                self.assertIsNone(campaign_name)
                self.assertIn(status, {"pending", "unavailable"})

    def test_auth_failure_opens_durable_circuit_and_prevents_another_call(self) -> None:
        self.stage()
        self.client = _Client(probe_error=MetaAdsError("meta_auth_unavailable"))
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        self.assertFalse(self.service.resolve_source("101", 20.0, 1.0))
        self.assertEqual(self.row()[:2], ("pending", "meta_auth_unavailable"))
        calls = list(self.client.calls)
        self.assertFalse(self.service.resolve_source("101", 21.0, 1.0))
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.service.health(21.0), "degraded")

    def test_health_requires_a_successful_probe_and_recovers_after_every_failure(
        self,
    ) -> None:
        self.assertEqual(self.service.health(20.0), "degraded")
        self.assertEqual(self.service.probe(20.0), "ready")
        self.assertEqual(self.service.health(20.0), "ready")

        for index, code in enumerate(
            (
                "meta_account_mismatch",
                "meta_required_tool_unavailable",
                "meta_timeout",
                "meta_server_unavailable",
            )
        ):
            with self.subTest(code=code):
                now = 21.0 + index
                self.client.probe_error = MetaAdsError(code)
                self.assertEqual(self.service.probe(now), "degraded")
                self.assertEqual(self.service.health(now), "degraded")
                self.client.probe_error = None
                self.assertEqual(self.service.probe(now + 0.5), "ready")
                self.assertEqual(self.service.health(now + 0.5), "ready")

    def test_revision_fenced_probe_close_never_marks_the_process_ready(self) -> None:
        self.client.on_probe = lambda: self.runtime.write(
            lambda conn: self.service._store.open_auth_circuit(conn, 20.0, 0.0)
        )

        self.assertEqual(self.service.probe(20.0), "degraded")
        self.assertEqual(self.service.health(81.0), "degraded")

    def test_expired_durable_auth_circuit_recreates_the_latched_remote_client(
        self,
    ) -> None:
        clock = [20.0]
        self.stage("event-recovery", "102")
        self.client = _Client(probe_error=MetaAdsError("meta_auth_unavailable"))
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        with patch("brain.meta_attribution.time.time", side_effect=lambda: clock[0]):
            self.assertEqual(self.service.probe(20.0), "degraded")
            self.client.probe_error = None
            self.client.ad = RemoteAd("102", "Ad", "202", "ACTIVE", "ACTIVE")
            clock[0] = 81.0
            self.assertTrue(self.service.resolve_source("102", 81.0, 1.0))
        self.assertEqual(self.row("event-recovery")[0], "confirmed")

    def test_claimed_lookup_auth_failure_resolves_after_circuit_probe_recovery(
        self,
    ) -> None:
        clock = [20.0]
        self.stage()
        self.client = _Client(ad_error=MetaAdsError("meta_auth_unavailable"))
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        with patch("brain.meta_attribution.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(self.service.resolve_source("101", 20.0))
            self.assertEqual(self.row()[:2], ("pending", "meta_auth_unavailable"))
            calls_after_failure = list(self.client.calls)
            self.client.ad_error = None

            clock[0] = 79.0
            self.assertEqual(self.service.tick(79.0), 0)
            self.assertEqual(self.client.calls, calls_after_failure)

            clock[0] = 81.0
            self.assertEqual(self.service.tick(81.0), 1)

        self.assertEqual(self.row()[0], "confirmed")

    def test_stale_successful_probe_cannot_clear_a_newer_auth_failure(self) -> None:
        self.stage()
        self.client = _Client()
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        self.client.on_probe = lambda: self.runtime.write(
            lambda conn: self.service._store.open_auth_circuit(conn, 20.0, 0.0)
        )
        self.assertTrue(self.service.resolve_source("101", 20.0, 1.0))
        self.assertEqual(self.service.health(20.0), "degraded")

    def test_account_and_required_tool_probe_failures_are_terminal_without_values(
        self,
    ) -> None:
        for index, code in enumerate(
            ("meta_account_mismatch", "meta_required_tool_unavailable")
        ):
            with self.subTest(code=code):
                source_id = str(111 + index)
                event_id = f"event-{code}"
                self.stage(event_id, source_id)
                client = _Client(probe_error=MetaAdsError(code))
                service = MetaAttributionService(self.settings, self.runtime, client)

                self.assertFalse(service.resolve_source(source_id, 20.0, 1.0))
                self.assertEqual(
                    self.row(event_id),
                    ("unavailable", code, None, None, None, None),
                )

    def test_contact_resolution_uses_the_shared_remaining_budget(self) -> None:
        self.stage("event-1", "101")
        self.stage("event-2", "102")
        self.client = _Client()
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        self.assertEqual(
            self.service.resolve_pending_for_contact(["event-1", "event-2"], 20.0, 1.0),
            1,
        )
        self.assertEqual(self.row("event-1")[0], "confirmed")
        self.assertEqual(self.row("event-2")[0], "pending")

    def test_timeout_is_retried_and_worker_claims_due_jobs(self) -> None:
        clock = [20.0]
        self.stage()
        self.client = _Client(ad_error=MetaAdsError("meta_timeout"))
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        with patch("brain.meta_attribution.time.time", side_effect=lambda: clock[0]):
            self.assertEqual(self.service.run_due_jobs(20.0), 0)
            self.assertEqual(self.row()[:2], ("pending", "meta_timeout"))
            due = self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT next_attempt_at FROM meta_attribution_jobs "
                    "WHERE source_id = '101'"
                ).fetchone()[0]
            )
            self.client = _Client()
            self.service = MetaAttributionService(
                self.settings, self.runtime, self.client
            )
            clock[0] = float(due)
            self.assertEqual(self.service.run_due_jobs(float(due)), 1)
        self.assertEqual(self.row()[0], "confirmed")

    def test_worker_refreshes_wall_clock_before_each_job_claim(self) -> None:
        clock = [20.0]

        class BatchClient(_Client):
            def get_ad(self, source_id: str, deadline: float | None = None) -> RemoteAd:
                self.calls.append(("ad", source_id, deadline))
                return RemoteAd(source_id, "Ad", "202", "ACTIVE", "ACTIVE")

            def get_campaign(
                self, campaign_id: str, deadline: float | None = None
            ) -> RemoteCampaign:
                campaign = super().get_campaign(campaign_id, deadline)
                if sum(call[0] == "campaign" for call in self.calls) == 1:
                    clock[0] = 22.0
                return campaign  # type: ignore[return-value]

        self.stage("event-1", "101")
        self.stage("event-2", "102")
        self.client = BatchClient()
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        with patch("brain.meta_attribution.time.time", side_effect=lambda: clock[0]):
            self.assertEqual(self.service.run_due_jobs(20.0, limit=2), 2)

        timestamps = self.runtime.read(
            lambda conn: [
                tuple(row)
                for row in conn.execute(
                    "SELECT source_id, last_attempt_at, confirmed_at "
                    "FROM ctwa_meta_attributions ORDER BY source_id"
                )
            ]
        )
        self.assertEqual(timestamps, [("101", 20.0, 22.0), ("102", 22.0, 22.0)])

    def test_completion_uses_advanced_wall_clock_and_cannot_cross_lease_expiry(
        self,
    ) -> None:
        clock = [20.0]

        class SlowCompletionClient(_Client):
            def get_campaign(
                self, campaign_id: str, deadline: float | None = None
            ) -> object:
                campaign = super().get_campaign(campaign_id, deadline)
                clock[0] = 26.0
                return campaign

        self.stage()
        self.client = SlowCompletionClient()
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        with patch("brain.meta_attribution.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(self.service.resolve_source("101", 20.0))

        self.assertEqual(self.row()[0], "pending")
        lease_until = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT lease_until FROM meta_attribution_jobs WHERE source_id = '101'"
            ).fetchone()[0]
        )
        self.assertLess(float(lease_until), clock[0])

    def test_failure_retry_uses_wall_clock_at_the_failure_write(self) -> None:
        clock = [20.0]

        class AdvancingFailureClient(_Client):
            def get_ad(self, source_id: str, deadline: float | None = None) -> object:
                self.calls.append(("ad", source_id, deadline))
                clock[0] = 22.0
                raise MetaAdsError("meta_timeout")

        self.stage()
        self.client = AdvancingFailureClient()
        self.service = MetaAttributionService(self.settings, self.runtime, self.client)

        with patch("brain.meta_attribution.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(self.service.resolve_source("101", 20.0))

        attribution = self.runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT reason_code, next_attempt_at, updated_at "
                    "FROM ctwa_meta_attributions WHERE source_id = '101'"
                ).fetchone()
            )
        )
        job = self.runtime.read(
            lambda conn: tuple(
                conn.execute(
                    "SELECT last_error_code, next_attempt_at, updated_at "
                    "FROM meta_attribution_jobs WHERE source_id = '101'"
                ).fetchone()
            )
        )
        self.assertEqual(attribution, ("meta_timeout", 82.0, 22.0))
        self.assertEqual(job, ("meta_timeout", 82.0, 22.0))

    def test_disabled_service_does_not_stage_or_call_the_remote_client(self) -> None:
        disabled = BrainSettings(
            principals=self.settings.principals,
            cursor_secret=b"c" * 32,
        )
        service = MetaAttributionService(disabled, self.runtime, self.client)

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO transport_events "
                "(event_id, observer_device_id, direction, received_at, transport_kind, created_at) "
                "VALUES ('event-disabled', 'observer', 'inbound', 1, 'ctwa_candidate', 1)"
            )
            service.stage_event(
                conn,
                event_id="event-disabled",
                raw={"sourceType": "ad", "sourceId": "101"},
                now=10.0,
            )

        self.runtime.write(write)
        self.assertEqual(
            self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM ctwa_meta_attributions"
                ).fetchone()[0]
            ),
            0,
        )
        self.assertFalse(service.resolve_source("101", 20.0))
        self.assertEqual(self.client.calls, [])
