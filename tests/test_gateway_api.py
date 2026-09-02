from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from brain import service
from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.gateway_api import GatewayAPI
from brain.mcp_server import BrainMCPServer, _tools
from brain.meta_ads_models import MetaAdRecord, MetaAdsError
from brain.meta_ads_store import MetaAdsStore
from brain.service import BrainService
from brain.transport_models import RuntimeIds


class _BlockingMetaClient:
    """Controlled remote double that honors the timeout passed by the resolver."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def get_ad(
        self,
        _source_id: str,
        _now: float,
        *,
        timeout_seconds: float | None = None,
    ) -> MetaAdRecord:
        assert timeout_seconds is not None
        self.timeouts.append(timeout_seconds)
        time.sleep(timeout_seconds)
        raise MetaAdsError("meta_timeout")


class _NonConformingContextResolver:
    """Resolver double that deliberately ignores the context budget."""

    def __init__(self) -> None:
        self.started = Event()

    def resolve_contact_pending(
        self, _event_id: str, _now: float, *, budget_seconds: float
    ) -> bool:
        self.started.set()
        time.sleep(service.CONTEXT_META_OPERATION_TIMEOUT_SECONDS + 0.5)
        return False


class GatewayAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_path = root / "state.db"
        self.kanban_path = root / "kanban.db"
        self.runtime_path = root / "runtime" / "brain-runtime.db"
        self.mapping_dir = root / "whatsapp-session"
        self.mapping_dir.mkdir()
        state = sqlite3.connect(self.state_path)
        state.executescript(
            """
            CREATE TABLE delivery_obligations (
                obligation_id TEXT PRIMARY KEY, session_key TEXT, platform TEXT,
                chat_id TEXT, content TEXT, state TEXT, created_at REAL,
                updated_at REAL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, session_key TEXT, source TEXT,
                chat_id TEXT, chat_type TEXT, started_at REAL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                role TEXT, content TEXT, timestamp REAL, active INTEGER,
                compacted INTEGER, display_kind TEXT, _compressed_summary INTEGER,
                tool_calls TEXT, tool_name TEXT
            );
            INSERT INTO sessions VALUES
                ('g-one', 'wa:g', 'whatsapp', '123456789012345@lid', 'dm', 1.0);
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
        settings = BrainSettings(
            state_db=self.state_path,
            kanban_db=self.kanban_path,
            whatsapp_session_dir=self.mapping_dir,
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-secret"),
                    frozenset({"conversation_phone", "conversation_context"}),
                ),
                "reno": PrincipalConfig(
                    "reno",
                    "worker",
                    token_digest("reno-secret"),
                    frozenset({"conversation_recent"}),
                ),
                "observer": PrincipalConfig(
                    "observer",
                    "service",
                    token_digest("observer-secret"),
                    frozenset({"transport_ingest"}),
                ),
            },
            cursor_secret=b"g" * 32,
            runtime_db=self.runtime_path,
            transport_hmac_secret=b"t" * 32,
        )
        self.service = BrainService(settings)
        self.api = GatewayAPI(self.service)
        self.ids = RuntimeIds(b"t" * 32)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def valid_context() -> dict[str, str]:
        return {
            "platform": "whatsapp",
            "chat_type": "dm",
            "chat_id": "123456789012345@lid",
            "session_key": "wa:g",
            "session_id": "g-one",
        }

    @staticmethod
    def request(
        payload: object,
        token: str = "gateway-secret",
        declared_length: int | None = None,
        *,
        path: str = "/internal/gateway/conversation-phone",
        method: str = "POST",
    ) -> Request:
        if isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload).encode("utf-8")
        sent = False

        async def receive() -> dict[str, object]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        headers = [
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
        ]
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
        }
        return Request(scope, receive)

    def post_gateway(
        self, payload: object, token: str = "gateway-secret"
    ) -> SimpleNamespace:
        response = asyncio.run(
            self.api.conversation_phone(self.request(payload, token))
        )
        return SimpleNamespace(
            status_code=response.status_code,
            text=response.body.decode("utf-8"),
            json=lambda: json.loads(response.body),
        )

    def post_context(
        self,
        payload: object,
        token: str = "gateway-secret",
        *,
        method: str = "POST",
    ) -> SimpleNamespace:
        response = asyncio.run(
            self.api.conversation_context(
                self.request(
                    payload,
                    token,
                    path="/internal/gateway/conversation-context",
                    method=method,
                )
            )
        )
        return SimpleNamespace(
            status_code=response.status_code,
            text=response.body.decode("utf-8"),
            json=lambda: json.loads(response.body),
        )

    def valid_turn(self) -> dict[str, object]:
        return {
            **self.valid_context(),
            "turn_id": "opaque-turn-1",
            "user_message": "hello",
            "turn_timestamp": 1000.0,
            # Matches the raw key.id seed_event() derives its event_id from.
            "message_ids": ["event-hello-"],
        }

    def prepare_turn_identity(self) -> None:
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_text(
            '"123456789012345"', encoding="utf-8"
        )

    def seed_event(
        self,
        body: str = "hello",
        *,
        transport_kind: str = "ordinary_inbound",
        source_app: str | None = None,
        external_ad_reply_raw_json: str | None = None,
        suffix: str = "",
        timestamp: float = 999.0,
    ) -> str:
        event_id = self.ids.event_id("observer-a", f"event-{body}-{suffix}")
        contact_key = self.ids.contact_key("5534999772714")
        self.service.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events (event_id, observer_device_id, "
                "contact_key, direction, received_at, message_timestamp, body_hmac, "
                "body_length, native_type, transport_kind, source_app, "
                "external_ad_reply_raw_json, created_at) "
                "VALUES (?, ?, ?, 'inbound', ?, ?, ?, ?, 'conversation', "
                "?, ?, ?, ?)",
                (
                    event_id,
                    "observer-a",
                    contact_key,
                    timestamp + 0.1,
                    timestamp,
                    self.ids.body_hmac(body),
                    len(body),
                    transport_kind,
                    source_app,
                    external_ad_reply_raw_json,
                    timestamp + 0.2,
                ),
            )
        )
        return event_id

    def context_payload(self) -> dict[str, str]:
        return self.valid_context()

    def enable_meta_attribution(
        self,
        *,
        clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> MetaAdsStore:
        settings = replace(
            self.service.settings,
            meta_attribution_enabled=True,
            meta_ad_account_id="act_1598606388477916",
            meta_ads_mcp_access_token="fixture-token",
        )
        self.service = BrainService(
            settings,
            clock=time.time if clock is None else clock,
            monotonic_clock=monotonic_clock,
        )
        self.api = GatewayAPI(self.service)
        return MetaAdsStore("1598606388477916")

    @staticmethod
    def meta_record(*, fetched_at: float) -> MetaAdRecord:
        return MetaAdRecord(
            account_id="1598606388477916",
            ad_id="120200000000001",
            ad_name="Lead ad",
            ad_status="PAUSED",
            ad_effective_status="ACTIVE",
            adset_id="120300000000001",
            adset_name="Prospecting",
            adset_status="ACTIVE",
            campaign_id="120400000000001",
            campaign_name="September leads",
            campaign_status="PAUSED",
            creative_id="120500000000001",
            creative_name="Image A",
            metadata_complete=True,
            fetched_at=fetched_at,
        )

    def test_gateway_context_resolves_phone_after_state_revalidation(self) -> None:
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_text(
            '"123456789012345"', encoding="utf-8"
        )

        response = self.post_gateway(self.valid_context())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "phone": "5534999772714"})

    def test_gateway_accepts_verified_phone_lid_longitudinal_alias(self) -> None:
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_text(
            '"123456789012345"', encoding="utf-8"
        )
        state = sqlite3.connect(self.state_path)
        state.execute(
            "INSERT INTO sessions VALUES "
            "('g-old', 'wa:g', 'whatsapp', '5534999772714@s.whatsapp.net', 'dm', 0.5)"
        )
        state.commit()
        state.close()

        response = self.post_gateway(self.valid_context())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "phone": "5534999772714"})

    def test_gateway_rejects_session_chat_id_mismatch(self) -> None:
        context = self.valid_context()
        context["chat_id"] = "other@lid"

        response = self.post_gateway(context)

        self.assertIn(response.status_code, {403, 409})
        self.assertNotIn("other@lid", response.text)

    def test_worker_token_cannot_use_gateway_endpoint(self) -> None:
        response = self.post_gateway(self.valid_context(), token="reno-secret")

        self.assertEqual(response.status_code, 403)

    def test_gateway_returns_sanitized_unavailable_mapping_result(self) -> None:
        response = self.post_gateway(self.valid_context())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"status": "unavailable", "reason": "phone_not_resolved"}
        )

    def test_gateway_rejects_invalid_contexts_without_echoing_body(self) -> None:
        cases = [
            ({"platform": "whatsapp"}, 400),
            ({**self.valid_context(), "extra": "secret-context"}, 400),
            ({**self.valid_context(), "platform": "telegram"}, 400),
            (
                {**self.valid_context(), "chat_type": "group", "chat_id": "group@g.us"},
                400,
            ),
            ({**self.valid_context(), "session_id": ""}, 400),
            ({**self.valid_context(), "session_key": "not-wa-g"}, 403),
        ]
        for payload, expected_status in cases:
            with self.subTest(payload=payload):
                response = self.post_gateway(payload)
                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn("secret-context", response.text)
                self.assertNotIn("not-wa-g", response.text)

    def test_gateway_rejects_placeholder_and_invalid_json(self) -> None:
        placeholder = self.post_gateway(
            self.valid_context(), token="${BRAIN_GATEWAY_TOKEN}"
        )
        invalid_json = self.post_gateway(b"{not-json")

        self.assertEqual(placeholder.status_code, 403)
        self.assertEqual(invalid_json.status_code, 400)
        self.assertNotIn("BRAIN_GATEWAY_TOKEN", placeholder.text)

    def test_gateway_rejects_oversized_declared_body_before_reading(self) -> None:
        response = asyncio.run(
            self.api.conversation_phone(
                self.request(self.valid_context(), declared_length=16_385)
            )
        )

        self.assertEqual(response.status_code, 400)

    def test_mcp_app_registers_private_gateway_route(self) -> None:
        app = BrainMCPServer(self.service).app()
        paths = {route.path for route in app.routes if hasattr(route, "path")}

        self.assertIn("/internal/gateway/conversation-phone", paths)

    def test_conversation_context_route_is_private_post_only(self) -> None:
        app = BrainMCPServer(self.service).app()
        routes = {route.path: route for route in app.routes if hasattr(route, "path")}

        self.assertIn("/internal/gateway/conversation-context", routes)
        self.assertEqual(
            routes["/internal/gateway/conversation-context"].methods, {"POST"}
        )
        self.assertNotIn("conversation_context", {tool.name for tool in _tools()})
        response = self.post_context(self.context_payload(), method="GET")
        self.assertEqual(response.status_code, 405)

    def test_turn_register_route_is_gone(self) -> None:
        """Amendment 2 removed turn registration; the route must not linger."""
        app = BrainMCPServer(self.service).app()
        paths = {route.path for route in app.routes if hasattr(route, "path")}

        self.assertNotIn("/internal/gateway/turn-register", paths)

    def test_conversation_context_returns_recent_transport_oldest_first(self) -> None:
        self.prepare_turn_identity()
        now = time.time()
        ordinary = self.seed_event("second", suffix="ordinary", timestamp=now - 100)
        ctwa = self.seed_event(
            "first",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            suffix="ctwa",
            timestamp=now - 200,
        )

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "contact": {
                    "phone_e164": "5534999772714",
                    "display_name": None,
                    "display_name_source": None,
                },
                "events": [
                    {
                        "event_id": ctwa,
                        "transport_kind": "ctwa_candidate",
                        "source_app": "instagram",
                        "inbound_kind": None,
                        "external_ad_reply": None,
                        "meta_attribution": None,
                    },
                    {
                        "event_id": ordinary,
                        "transport_kind": "ordinary_inbound",
                        "source_app": None,
                        "inbound_kind": None,
                        "external_ad_reply": None,
                        "meta_attribution": None,
                    },
                ],
            },
        )
        self.assertNotIn("turn", response.json())
        serialized = response.text
        for private_field in (
            "body_hmac",
            "contact_key",
            "session_key",
            "hermes_session_id",
            "source_id_hmac",
            "ctwa_clid_hmac",
        ):
            self.assertNotIn(private_field, serialized)

    def test_conversation_context_returns_exact_raw_attribution(self) -> None:
        self.prepare_turn_identity()
        self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"ctwa-clid","sourceId":"source-id","thumbnail":'
                '{"$type":"bytes","data":"AAEC/w==","encoding":"base64"}}'
            ),
            suffix="raw",
            timestamp=time.time() - 10,
        )

        event = self.post_context(self.context_payload()).json()["events"][0]

        self.assertEqual(
            event["external_ad_reply"],
            {
                "sourceId": "source-id",
                "ctwaClid": "ctwa-clid",
                "thumbnail": {
                    "$type": "bytes",
                    "encoding": "base64",
                    "data": "AAEC/w==",
                },
            },
        )
        self.assertIsNone(event["meta_attribution"])

    def test_conversation_context_exposes_confirmed_exact_meta_attribution(
        self,
    ) -> None:
        """Omitting a proven ad would prevent the CEO from identifying a CTWA lead."""
        self.prepare_turn_identity()
        store = self.enable_meta_attribution(clock=lambda: 1_700_000_050.0)
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="confirmed-meta",
            timestamp=1_700_000_000.0,
        )
        record = self.meta_record(fetched_at=1_700_000_020.0)
        self.service.runtime.write(
            lambda conn: store.upsert_record_and_confirm(
                conn, record, confirmed_at=1_700_000_021.0
            )
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={
                    "sourceType": "ad",
                    "sourceId": "120200000000001",
                    "ctwaClid": "clid-123",
                },
                now=1_700_000_022.0,
            )
        )

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(
            response.json()["events"][0]["meta_attribution"],
            {
                "status": "confirmed",
                "account_id": "act_1598606388477916",
                "matched_by": "source_id_exact",
                "source_id": "120200000000001",
                "ctwa_clid": "clid-123",
                "ad": {
                    "id": "120200000000001",
                    "name": "Lead ad",
                    "status": "ACTIVE",
                },
                "adset": {
                    "id": "120300000000001",
                    "name": "Prospecting",
                    "status": "ACTIVE",
                },
                "campaign": {
                    "id": "120400000000001",
                    "name": "September leads",
                    "status": "PAUSED",
                },
                "creative": {
                    "id": "120500000000001",
                    "name": "Image A",
                },
                "metadata_complete": True,
                "confirmed_at": "2023-11-14T22:13:42Z",
                "metadata_fetched_at": "2023-11-14T22:13:40Z",
            },
        )

    def test_conversation_context_exposes_pending_meta_attribution(
        self,
    ) -> None:
        """Treating an unresolved CTWA source as null would hide durable retry state."""
        self.prepare_turn_identity()
        self.enable_meta_attribution()
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="pending-meta",
            timestamp=time.time() - 10,
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={
                    "sourceType": "ad",
                    "sourceId": "120200000000001",
                    "ctwaClid": "clid-123",
                },
                now=time.time() - 5,
            )
        )
        self.service.meta_attribution = SimpleNamespace(
            enabled=True,
            resolve_contact_pending=lambda _event_id, _now, *, budget_seconds: False,
        )

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(
            response.json()["events"][0]["meta_attribution"],
            {
                "status": "pending",
                "source_id": "120200000000001",
                "ctwa_clid": "clid-123",
                "last_attempt_at": None,
                "retry_scheduled": True,
                "last_error_code": None,
            },
        )

    def test_context_lookup_stops_at_the_remaining_deadline_and_keeps_raw_pending(
        self,
    ) -> None:
        """A slow Meta read must not make the CEO lose the transport context."""
        self.prepare_turn_identity()
        clock_values = iter((100.0, 100.0, 104.98, 104.99, 104.995))
        self.enable_meta_attribution(
            clock=lambda: 1_700_000_050.0,
            monotonic_clock=lambda: next(clock_values),
        )
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="bounded-meta",
            timestamp=1_700_000_000.0,
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": "120200000000001"},
                now=1_700_000_010.0,
            )
        )
        blocking_client = _BlockingMetaClient()
        attribution._client = blocking_client  # type: ignore[assignment]

        started = time.perf_counter()
        response = self.post_context(self.context_payload())
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.5)
        event = response.json()["events"][0]
        self.assertEqual(event["external_ad_reply"]["sourceId"], "120200000000001")
        self.assertEqual(event["meta_attribution"]["status"], "pending")
        self.assertIn(
            event["meta_attribution"]["last_error_code"], {None, "meta_timeout"}
        )

    def test_context_deadline_does_not_wait_for_a_nonconforming_resolver(
        self,
    ) -> None:
        """An injected resolver that ignores its budget must not hold the CEO past 5s."""
        self.prepare_turn_identity()
        self.enable_meta_attribution()
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="nonconforming-resolver",
            timestamp=time.time() - 10,
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": "120200000000001"},
                now=time.time() - 5,
            )
        )
        resolver = _NonConformingContextResolver()
        self.service.meta_attribution = SimpleNamespace(
            enabled=True, resolve_contact_pending=resolver.resolve_contact_pending
        )

        started = time.perf_counter()
        response = self.post_context(self.context_payload())
        elapsed = time.perf_counter() - started

        self.assertTrue(resolver.started.is_set())
        self.assertLess(elapsed, service.CONTEXT_META_OPERATION_TIMEOUT_SECONDS + 0.3)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(
            response.json()["events"][0]["meta_attribution"]["status"], "pending"
        )

    def test_context_rereads_after_immediate_resolution_before_rendering(
        self,
    ) -> None:
        """Using a pre-resolution view would hide a worker confirmation race."""
        self.prepare_turn_identity()
        store = self.enable_meta_attribution()
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="resolution-race",
            timestamp=time.time() - 10,
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": "120200000000001"},
                now=time.time() - 5,
            )
        )
        record = self.meta_record(fetched_at=time.time() - 1)

        def confirm(_event_id: str, now: float, *, budget_seconds: float) -> bool:
            self.service.runtime.write(
                lambda conn: store.upsert_record_and_confirm(
                    conn, record, confirmed_at=now
                )
            )
            return True

        self.service.meta_attribution = SimpleNamespace(
            enabled=True, resolve_contact_pending=confirm
        )

        event = self.post_context(self.context_payload()).json()["events"][0]

        self.assertEqual(event["meta_attribution"]["status"], "confirmed")
        self.assertEqual(event["meta_attribution"]["ad"]["id"], "120200000000001")

    def test_context_rereads_when_worker_confirms_before_pending_selection(
        self,
    ) -> None:
        """A worker confirmation after the snapshot must not render stale pending."""
        self.prepare_turn_identity()
        store = self.enable_meta_attribution()
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="selection-race",
            timestamp=time.time() - 10,
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": "120200000000001"},
                now=time.time() - 5,
            )
        )
        record = self.meta_record(fetched_at=time.time() - 1)

        def confirm_before_selection(**_: object) -> None:
            self.service.runtime.write(
                lambda conn: store.upsert_record_and_confirm(
                    conn, record, confirmed_at=time.time()
                )
            )

        with patch.object(
            self.service,
            "_pending_context_meta_event_id_until_deadline",
            side_effect=confirm_before_selection,
        ):
            event = self.post_context(self.context_payload()).json()["events"][0]

        self.assertEqual(event["meta_attribution"]["status"], "confirmed")
        self.assertEqual(event["meta_attribution"]["ad"]["id"], "120200000000001")

    def test_context_downgrades_an_inconsistent_stored_confirmation_to_pending(
        self,
    ) -> None:
        """A missing catalog proof must never be rendered as a named Meta ad."""
        self.prepare_turn_identity()
        store = self.enable_meta_attribution()
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="inconsistent-confirmation",
            timestamp=time.time() - 10,
        )
        record = self.meta_record(fetched_at=time.time() - 1)
        self.service.runtime.write(
            lambda conn: store.upsert_record_and_confirm(
                conn, record, confirmed_at=time.time()
            )
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": "120200000000001"},
                now=time.time(),
            )
        )
        self.service.runtime.write(
            lambda conn: conn.execute(
                "DELETE FROM meta_ads_catalog WHERE account_id = ? AND ad_id = ?",
                ("1598606388477916", "120200000000001"),
            )
        )

        event = self.post_context(self.context_payload()).json()["events"][0]

        self.assertEqual(event["meta_attribution"]["status"], "pending")
        self.assertEqual(
            event["meta_attribution"]["last_error_code"], "meta_invalid_response"
        )
        self.assertNotIn("ad", event["meta_attribution"])

    def test_context_rejects_the_complete_expanded_meta_response_when_too_large(
        self,
    ) -> None:
        """Size enforcement must include Meta metadata, not only raw CTWA bytes."""
        self.prepare_turn_identity()
        store = self.enable_meta_attribution()
        event_id = self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json=(
                '{"ctwaClid":"clid-123","sourceId":"120200000000001","sourceType":"ad"}'
            ),
            suffix="oversized-meta",
            timestamp=time.time() - 10,
        )
        record = MetaAdRecord(
            account_id="1598606388477916",
            ad_id="120200000000001",
            ad_name="a" * 512,
            ad_status=None,
            ad_effective_status=None,
            adset_id="120300000000001",
            adset_name="b" * 512,
            adset_status=None,
            campaign_id="120400000000001",
            campaign_name="c" * 512,
            campaign_status=None,
            creative_id="120500000000001",
            creative_name="d" * 512,
            metadata_complete=True,
            fetched_at=time.time() - 1,
        )
        self.service.runtime.write(
            lambda conn: store.upsert_record_and_confirm(
                conn, record, confirmed_at=time.time()
            )
        )
        attribution = self.service.meta_attribution
        assert attribution is not None
        self.service.runtime.write(
            lambda conn: attribution.stage_event(
                conn,
                event_id=event_id,
                raw={"sourceType": "ad", "sourceId": "120200000000001"},
                now=time.time(),
            )
        )
        object.__setattr__(self.service.settings, "context_response_max_bytes", 1024)

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "reason": "context_too_large"},
        )

    def test_conversation_context_returns_unavailable_for_invalid_stored_raw(
        self,
    ) -> None:
        self.prepare_turn_identity()
        self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json='{"secret":"fixture-secret"',
            suffix="invalid-raw",
            timestamp=time.time() - 10,
        )

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "reason": "context_unavailable"},
        )
        self.assertNotIn("fixture-secret", response.text)

    def test_conversation_context_contains_stored_json_decoder_failures(self) -> None:
        self.prepare_turn_identity()
        self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json='{"value":' + "1" * 5_000 + "}",
            suffix="decoder-limit",
            timestamp=time.time() - 10,
        )

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "reason": "context_unavailable"},
        )
        self.assertNotIn("1" * 100, response.text)

    def test_conversation_context_refuses_the_complete_oversized_response(
        self,
    ) -> None:
        self.prepare_turn_identity()
        self.seed_event(
            "ad click",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            external_ad_reply_raw_json='{"unknown":"' + "x" * 300 + '"}',
            suffix="oversized-context",
            timestamp=time.time() - 10,
        )
        object.__setattr__(self.service.settings, "context_response_max_bytes", 256)

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "reason": "context_too_large"},
        )

    def test_conversation_context_excludes_transport_outside_the_window(self) -> None:
        """A contact's older transport is a profile, not the current context."""
        self.prepare_turn_identity()
        now = time.time()
        recent = self.seed_event("recent", suffix="recent", timestamp=now - 60)
        self.seed_event(
            "ancient",
            transport_kind="ctwa_candidate",
            source_app="facebook",
            suffix="ancient",
            timestamp=now - service.CONTEXT_WINDOW_SECONDS - 60,
        )

        events = self.post_context(self.context_payload()).json()["events"]

        self.assertEqual([event["event_id"] for event in events], [recent])

    def test_conversation_context_is_bounded_in_count(self) -> None:
        self.prepare_turn_identity()
        now = time.time()
        for index in range(service.CONTEXT_MAX_EVENTS + 4):
            self.seed_event("body", suffix=f"e{index}", timestamp=now - index)

        events = self.post_context(self.context_payload()).json()["events"]

        self.assertEqual(len(events), service.CONTEXT_MAX_EVENTS)

    def test_conversation_context_returns_only_valid_display_name_ephemera(
        self,
    ) -> None:
        self.prepare_turn_identity()
        now = time.time()
        for suffix, expires_at, expected in (
            ("valid", 4_000_000_000.0, ("Maria Silva", "whatsapp_profile")),
            ("expired", 1.0, (None, None)),
        ):
            with self.subTest(suffix=suffix):
                self.seed_event("body", suffix=suffix, timestamp=now - 10)
                self.service.runtime.write(
                    lambda conn, expires_at=expires_at: conn.execute(
                        "INSERT OR REPLACE INTO contact_ephemera "
                        "(contact_key, display_name, display_name_hmac, expires_at, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            self.ids.contact_key("5534999772714"),
                            "Maria Silva",
                            self.ids.opaque_hmac("Maria Silva"),
                            expires_at,
                            1.0,
                            1.0,
                        ),
                    )
                )
                contact = self.post_context(self.context_payload()).json()["contact"]
                self.assertEqual(
                    (contact["display_name"], contact["display_name_source"]), expected
                )

    def test_conversation_context_unavailable_without_recent_transport(self) -> None:
        self.prepare_turn_identity()

        response = self.post_context(self.context_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "reason": "no_recent_transport"},
        )

    def test_conversation_context_rejects_an_unknown_field(self) -> None:
        response = self.post_context(
            {**self.context_payload(), "wa_turn_id": "waturn_gone"}
        )

        self.assertNotEqual(response.status_code, 200)

    def test_conversation_context_denies_wrong_capability_before_body_read(
        self,
    ) -> None:
        body_read = False

        async def receive() -> dict[str, object]:
            nonlocal body_read
            body_read = True
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/gateway/conversation-context",
                "headers": [(b"authorization", b"Bearer reno-secret")],
            },
            receive,
        )
        response = asyncio.run(self.api.conversation_context(request))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(body_read)


if __name__ == "__main__":
    unittest.main()
