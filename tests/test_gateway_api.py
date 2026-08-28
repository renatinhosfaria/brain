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
from brain.mcp_server import BrainMCPServer
from brain.service import BrainService


class GatewayAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_path = root / "state.db"
        self.kanban_path = root / "kanban.db"
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
                    frozenset({"conversation_phone"}),
                ),
                "reno": PrincipalConfig(
                    "reno",
                    "worker",
                    token_digest("reno-secret"),
                    frozenset({"conversation_recent"}),
                ),
            },
            cursor_secret=b"g" * 32,
        )
        self.service = BrainService(settings)
        self.api = GatewayAPI(self.service)

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
            "method": "POST",
            "path": "/internal/gateway/conversation-phone",
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


if __name__ == "__main__":
    unittest.main()
