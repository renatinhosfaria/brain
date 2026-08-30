from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.gateway_api import GatewayAPI
from brain.mcp_server import BrainMCPServer, _tools
from brain.service import BrainService
from brain.transport_models import RuntimeIds
from brain.turn_correlation import session_key_hmac


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
                current_run_id INTEGER, session_id TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_id TEXT, status TEXT
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
                    frozenset(
                        {"conversation_phone", "turn_register", "conversation_context"}
                    ),
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
            runtime_hmac_secret=b"r" * 32,
            transport_hmac_secret=b"t" * 32,
        )
        self.service = BrainService(settings)
        self.api = GatewayAPI(self.service)
        self.ids = RuntimeIds(b"r" * 32, b"t" * 32)

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

    def post_turn(
        self,
        payload: object,
        token: str = "gateway-secret",
        *,
        method: str = "POST",
    ) -> SimpleNamespace:
        response = asyncio.run(
            self.api.turn_register(
                self.request(
                    payload,
                    token,
                    path="/internal/gateway/turn-register",
                    method=method,
                )
            )
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
        suffix: str = "",
        timestamp: float = 999.0,
    ) -> str:
        event_id = self.ids.event_id("observer-a", f"event-{body}-{suffix}")
        contact_key = self.ids.contact_key("5534999772714")
        self.service.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events (event_id, observer_device_id, "
                "contact_key, direction, received_at, message_timestamp, body_hmac, "
                "body_length, native_type, transport_kind, source_app, created_at) "
                "VALUES (?, ?, ?, 'inbound', ?, ?, ?, ?, 'conversation', "
                "?, ?, ?)",
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
                    timestamp + 0.2,
                ),
            )
        )
        return event_id

    def context_payload(self, wa_turn_id: str) -> dict[str, str]:
        return {**self.valid_context(), "wa_turn_id": wa_turn_id}

    def insert_turn(self, status: str, turn_id: str = "opaque-context") -> str:
        wa_turn_id = self.ids.wa_turn_id(turn_id)
        self.service.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wa_turn_id,
                    "g-one",
                    session_key_hmac(b"r" * 32, "wa:g"),
                    self.ids.contact_key("5534999772714"),
                    self.ids.body_hmac("body"),
                    4,
                    1000.0,
                    status,
                    1000.1,
                ),
            )
        )
        return wa_turn_id

    def link_event(self, wa_turn_id: str, event_id: str, ordinal: int) -> None:
        self.service.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO turn_events (wa_turn_id, event_id, ordinal) VALUES (?, ?, ?)",
                (wa_turn_id, event_id, ordinal),
            )
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

    def test_turn_register_route_correlates_and_is_not_model_visible(self) -> None:
        self.prepare_turn_identity()
        self.seed_event()

        response = self.post_turn(self.valid_turn())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["correlation"], "correlated")
        app = BrainMCPServer(self.service).app()
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        self.assertIn("/internal/gateway/turn-register", paths)
        self.assertNotIn("turn_register", {tool.name for tool in _tools()})

    def test_turn_register_denies_worker_service_and_invalid_tokens(self) -> None:
        self.prepare_turn_identity()
        for token in ("reno-secret", "observer-secret", "invalid"):
            with self.subTest(token=token):
                response = self.post_turn(self.valid_turn(), token=token)
                self.assertEqual(response.status_code, 403)

    def test_turn_register_authenticates_before_reading_body(self) -> None:
        body_read = False

        async def receive() -> dict[str, object]:
            nonlocal body_read
            body_read = True
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/gateway/turn-register",
                "headers": [(b"authorization", b"Bearer invalid")],
            },
            receive,
        )
        response = asyncio.run(self.api.turn_register(request))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(body_read)

    def test_turn_register_rejects_unknown_invalid_scope_timestamp_and_get(
        self,
    ) -> None:
        self.prepare_turn_identity()
        cases = (
            ({**self.valid_turn(), "unknown": True}, "POST"),
            ({**self.valid_turn(), "turn_timestamp": True}, "POST"),
            ({**self.valid_turn(), "turn_timestamp": float("inf")}, "POST"),
            ({**self.valid_turn(), "platform": "telegram"}, "POST"),
            ({**self.valid_turn(), "chat_type": "group"}, "POST"),
            (self.valid_turn(), "GET"),
        )
        for payload, method in cases:
            with self.subTest(payload=payload, method=method):
                response = self.post_turn(payload, method=method)
                self.assertIn(response.status_code, {400, 405})

    def test_conversation_context_route_is_private_post_only(self) -> None:
        app = BrainMCPServer(self.service).app()
        routes = {route.path: route for route in app.routes if hasattr(route, "path")}

        self.assertIn("/internal/gateway/conversation-context", routes)
        self.assertEqual(
            routes["/internal/gateway/conversation-context"].methods, {"POST"}
        )
        self.assertNotIn("conversation_context", {tool.name for tool in _tools()})
        response = self.post_context(
            self.context_payload("waturn_missing"), method="GET"
        )
        self.assertEqual(response.status_code, 405)

    def test_conversation_context_returns_ordered_safe_transport_facts(self) -> None:
        self.prepare_turn_identity()
        wa_turn_id = self.insert_turn("correlated")
        ordinary = self.seed_event("second", suffix="ordinary", timestamp=999.2)
        ctwa = self.seed_event(
            "first",
            transport_kind="ctwa_candidate",
            source_app="instagram",
            suffix="ctwa",
            timestamp=999.1,
        )
        self.link_event(wa_turn_id, ordinary, 1)
        self.link_event(wa_turn_id, ctwa, 0)

        response = self.post_context(self.context_payload(wa_turn_id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "contact": {
                    "phone_e164": "5534999772714",
                    "display_name": None,
                    "display_name_source": None,
                },
                "turn": {"wa_turn_id": wa_turn_id},
                "events": [
                    {
                        "event_id": ctwa,
                        "transport_kind": "ctwa_candidate",
                        "source_app": "instagram",
                        "inbound_kind": None,
                    },
                    {
                        "event_id": ordinary,
                        "transport_kind": "ordinary_inbound",
                        "source_app": None,
                        "inbound_kind": None,
                    },
                ],
            },
        )
        event_fields = {"event_id", "transport_kind", "source_app", "inbound_kind"}
        self.assertTrue(
            all(set(event) == event_fields for event in response.json()["events"])
        )
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

    def test_conversation_context_returns_only_valid_display_name_ephemera(
        self,
    ) -> None:
        self.prepare_turn_identity()
        for suffix, expires_at, expected in (
            ("valid", 4_000_000_000.0, ("Maria Silva", "whatsapp_profile")),
            ("expired", 1.0, (None, None)),
        ):
            with self.subTest(suffix=suffix):
                wa_turn_id = self.insert_turn("correlated", f"turn-{suffix}")
                event_id = self.seed_event("body", suffix=suffix)
                self.link_event(wa_turn_id, event_id, 0)
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
                response = self.post_context(self.context_payload(wa_turn_id))
                contact = response.json()["contact"]
                self.assertEqual(
                    (contact["display_name"], contact["display_name_source"]), expected
                )

    def test_conversation_context_missing_pending_ambiguous_or_wrong_turn_unavailable(
        self,
    ) -> None:
        self.prepare_turn_identity()
        for status in ("pending", "ambiguous"):
            with self.subTest(status=status):
                wa_turn_id = self.insert_turn(status, f"turn-{status}")
                response = self.post_context(self.context_payload(wa_turn_id))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "unavailable")
        missing = self.post_context(self.context_payload("waturn_missing"))
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json()["status"], "unavailable")

        other_context = self.valid_context()
        other_context["session_id"] = "other"
        wrong = self.post_context({**other_context, "wa_turn_id": "waturn_missing"})
        self.assertNotEqual(wrong.status_code, 200)

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
