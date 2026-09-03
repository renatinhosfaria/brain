from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import service
from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.mcp_server import BrainMCPServer
from brain.meta_ads_models import MetaAdsError, RemoteAd, RemoteCampaign
from brain.raw_attribution import RawAttributionLimits
from brain.service import BrainService
from brain.transport_models import RuntimeIds


class _Client:
    def __init__(self, *, error: MetaAdsError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str | None, float | None]] = []
        self.closed = False

    def probe(self, deadline: float | None = None) -> None:
        self.calls.append(("probe", None, deadline))
        if self.error:
            raise self.error

    def get_ad(self, source_id: str, deadline: float | None = None) -> RemoteAd:
        self.calls.append(("ad", source_id, deadline))
        return RemoteAd(source_id, "Confirmed Ad", "202", "ACTIVE", "ACTIVE")

    def get_campaign(
        self, campaign_id: str, deadline: float | None = None
    ) -> RemoteCampaign:
        self.calls.append(("campaign", campaign_id, deadline))
        return RemoteCampaign(campaign_id, "Confirmed Campaign", "ACTIVE", "ACTIVE")

    def invalidate(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class MetaContextIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.mapping_dir = root / "whatsapp-session"
        self.mapping_dir.mkdir()
        self.state_path = root / "state.db"
        self.kanban_path = root / "kanban.db"
        self.runtime_path = root / "runtime.db"
        self._create_authorization_databases()
        self.client = _Client()
        self.settings = BrainSettings(
            state_db=self.state_path,
            kanban_db=self.kanban_path,
            runtime_db=self.runtime_path,
            whatsapp_session_dir=self.mapping_dir,
            transport_hmac_secret=b"t" * 32,
            cursor_secret=b"c" * 32,
            meta_ads_mcp_enabled=True,
            meta_ads_mcp_api_key="test-key",
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-secret"),
                    frozenset({"conversation_context"}),
                )
            },
        )
        with patch.object(service, "RemoteMetaAdsMcpClient", return_value=self.client):
            self.brain = BrainService(self.settings)
        self.ids = RuntimeIds(b"t" * 32)
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_text(
            '"123456789012345"', encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_authorization_databases(self) -> None:
        state = sqlite3.connect(self.state_path)
        state.executescript(
            """
            CREATE TABLE delivery_obligations (
                obligation_id TEXT PRIMARY KEY, session_key TEXT, platform TEXT,
                chat_id TEXT, content TEXT, state TEXT, created_at REAL, updated_at REAL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, session_key TEXT, source TEXT,
                chat_id TEXT, chat_type TEXT, started_at REAL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                timestamp REAL, active INTEGER, compacted INTEGER, display_kind TEXT,
                _compressed_summary INTEGER, tool_calls TEXT, tool_name TEXT
            );
            INSERT INTO sessions VALUES
                ('g-one', 'wa:g', 'whatsapp', '123456789012345@lid', 'dm', 1);
            """
        )
        state.commit()
        state.close()
        kanban = sqlite3.connect(self.kanban_path)
        kanban.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, assignee TEXT, status TEXT,
                current_run_id INTEGER, session_id TEXT, idempotency_key TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_id TEXT, status TEXT,
                summary TEXT, metadata TEXT, started_at REAL, ended_at REAL
            );
            CREATE TABLE kanban_notify_subs (
                task_id TEXT, platform TEXT, chat_id TEXT,
                chat_type TEXT, notifier_profile TEXT
            );
            """
        )
        kanban.commit()
        kanban.close()

    def _seed_event(
        self,
        event_id: str,
        *,
        kind: str = "ctwa_candidate",
        received_at: float | None = None,
    ) -> None:
        now = time.time() if received_at is None else received_at
        contact_key = self.ids.contact_key("5534999772714")
        self.brain.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events "
                "(event_id, observer_device_id, contact_key, direction, received_at, "
                "transport_kind, created_at) VALUES (?, 'observer', ?, 'inbound', ?, ?, ?)",
                (event_id, contact_key, now, kind, now),
            )
        )

    def _stage(self, event_id: str, source_id: str = "101") -> None:
        self.brain.runtime.write(
            lambda conn: self.brain.meta_attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": source_id, "ctwaClid": "clid"},
                now=time.time(),
            )
        )

    def _context(self) -> dict[str, object]:
        return self.brain.gateway_conversation_context(
            {"Authorization": "Bearer gateway-secret"},
            service.GatewaySessionContext(
                platform="whatsapp",
                chat_type="dm",
                chat_id="123456789012345@lid",
                session_key="wa:g",
                session_id="g-one",
            ),
        )

    def _projection(self) -> dict[str, object]:
        return self.brain.runtime.read(
            lambda conn: self.brain._conversation_context_from_runtime(
                conn,
                contact_key=self.ids.contact_key("5534999772714"),
                phone_e164="5534999772714",
                now=time.time(),
                raw_limits=RawAttributionLimits(),
            )
        )

    def test_disabled_startup_does_not_construct_or_call_the_remote_client(self) -> None:
        disabled = BrainSettings(
            state_db=self.state_path,
            kanban_db=self.kanban_path,
            runtime_db=Path(self.temp_dir.name) / "disabled-runtime.db",
            whatsapp_session_dir=self.mapping_dir,
            transport_hmac_secret=b"t" * 32,
            cursor_secret=b"d" * 32,
            principals=self.settings.principals,
        )
        with patch.object(service, "RemoteMetaAdsMcpClient") as remote:
            brain = BrainService(disabled)
        self.assertEqual(brain.health().meta_ads_mcp, "disabled")
        remote.assert_not_called()

    def test_health_is_additive_and_missing_key_is_degraded(self) -> None:
        self.assertEqual(self.brain.health().status, "ok")
        self.assertEqual(self.brain.health().meta_ads_mcp, "ready")
        missing_key = BrainSettings(
            state_db=self.state_path,
            kanban_db=self.kanban_path,
            runtime_db=Path(self.temp_dir.name) / "missing-key.db",
            whatsapp_session_dir=self.mapping_dir,
            transport_hmac_secret=b"t" * 32,
            cursor_secret=b"m" * 32,
            meta_ads_mcp_enabled=True,
            principals=self.settings.principals,
        )
        brain = BrainService(missing_key)
        self.assertEqual(brain.health().status, "ok")
        self.assertEqual(brain.health().meta_ads_mcp, "degraded")

    def test_worker_tick_respects_the_durable_auth_circuit(self) -> None:
        self._seed_event("pending")
        self._stage("pending")
        self.brain.runtime.write(
            lambda conn: self.brain.meta_attribution._store.open_auth_circuit(
                conn, time.time(), 60.0
            )
        )

        self.assertEqual(self.brain.meta_attribution.tick(time.time()), 0)
        self.assertEqual(self.client.calls, [])

    def test_context_projects_only_confirmed_meta_names(self) -> None:
        self._seed_event("confirmed")
        self._stage("confirmed")
        self.assertTrue(self.brain.meta_attribution.resolve_source("101", time.time()))
        self._seed_event("pending")
        self._stage("pending", "102")
        self._seed_event("unavailable")
        self._stage("unavailable", "103")
        self.brain.runtime.write(
            lambda conn: conn.execute(
                "UPDATE ctwa_meta_attributions SET status='unavailable', "
                "reason_code='meta_not_found' WHERE event_id='unavailable'"
            )
        )
        self._seed_event("ordinary", kind="ordinary_inbound")

        events = {event["event_id"]: event for event in self._projection()["events"]}

        self.assertEqual(
            events["confirmed"]["meta_attribution"],
            {
                "status": "confirmed",
                "ad_id": "101",
                "ad_name": "Confirmed Ad",
                "campaign_id": "202",
                "campaign_name": "Confirmed Campaign",
            },
        )
        self.assertEqual(events["pending"]["meta_attribution"], {"status": "pending"})
        self.assertEqual(
            events["unavailable"]["meta_attribution"],
            {"status": "unavailable", "reason": "meta_not_found"},
        )
        self.assertNotIn("meta_attribution", events["ordinary"])

    def test_context_resolves_only_the_newest_pending_ctwa_source(self) -> None:
        self._seed_event("older", received_at=time.time() - 2)
        self._stage("older", "101")
        self._seed_event("newest", received_at=time.time() - 1)
        self._stage("newest", "102")

        result = self._context()

        events = {event["event_id"]: event for event in result["events"]}
        self.assertEqual(events["newest"]["meta_attribution"]["status"], "confirmed")
        self.assertEqual(events["older"]["meta_attribution"]["status"], "pending")
        self.assertEqual([call[:2] for call in self.client.calls], [
            ("probe", None), ("ad", "102"), ("campaign", "202")
        ])

    def test_context_remote_failure_is_fail_open_and_does_not_log_sensitive_values(self) -> None:
        self.client.error = MetaAdsError("meta_server_unavailable")
        self._seed_event("pending")
        self._stage("pending", "101")
        with self.assertLogs("brain.audit", level="INFO") as captured:
            result = self._context()

        event = result["events"][0]
        self.assertEqual(event["meta_attribution"]["status"], "pending")
        output = "\n".join(captured.output)
        self.assertNotIn("101", output)
        self.assertNotIn("test-key", output)
        self.assertNotIn("Confirmed Ad", output)

    def test_expired_context_budget_returns_pending_raw_context_without_remote_work(self) -> None:
        self._seed_event("pending")
        self._stage("pending", "101")

        self.brain._resolve_newest_pending_context_source(
            self.ids.contact_key("5534999772714"),
            time.time(),
            time.monotonic() - 0.001,
        )

        self.assertEqual(self.client.calls, [])
        self.assertEqual(
            self._projection()["events"][0]["meta_attribution"],
            {"status": "pending"},
        )

    def test_context_does_not_resolve_a_pending_ctwa_outside_the_context_window(self) -> None:
        self._seed_event(
            "old-pending",
            received_at=time.time() - service.CONTEXT_WINDOW_SECONDS - 1,
        )
        self._stage("old-pending", "101")

        result = self._context()

        self.assertEqual(
            result, {"status": "unavailable", "reason": "no_recent_transport"}
        )
        self.assertEqual(self.client.calls, [])

    def test_lifespan_ticks_cancels_and_closes_the_client(self) -> None:
        app = BrainMCPServer(self.brain).app()
        object.__setattr__(
            self.brain.settings, "meta_ads_mcp_worker_interval_seconds", 0.01
        )

        async def lifespan() -> None:
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.03)

        asyncio.run(lifespan())

        self.assertTrue(self.client.calls)
        self.assertTrue(self.client.closed)


if __name__ == "__main__":
    unittest.main()
