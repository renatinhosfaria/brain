from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.mcp_server import BrainMCPServer, _tools
from brain.service import BrainService
from brain.transport_api import TransportAPI
from brain.transport_models import RuntimeIds


class TransportIngestTests(unittest.TestCase):
    PHONE = "19999999999"
    LID = "123456789012345"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_path = root / "state.db"
        self.kanban_path = root / "kanban.db"
        self.runtime_path = root / "runtime" / "brain-runtime.db"
        self.observer_dir = root / "observer-session"
        self.observer_dir.mkdir()
        (self.observer_dir / f"lid-mapping-{self.PHONE}.json").write_text(
            json.dumps(self.LID), encoding="utf-8"
        )
        self.runtime_ids = RuntimeIds(b"r" * 32, b"t" * 32)
        self.settings = BrainSettings(
            state_db=self.state_path,
            kanban_db=self.kanban_path,
            runtime_db=self.runtime_path,
            observer_session_dir=self.observer_dir,
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-secret"),
                    frozenset({"conversation_context", "turn_register"}),
                ),
                "observer": PrincipalConfig(
                    "observer",
                    "service",
                    token_digest("observer-secret"),
                    frozenset({"transport_ingest"}),
                ),
                "writer": PrincipalConfig(
                    "writer",
                    "service",
                    token_digest("writer-secret"),
                    frozenset({"lifecycle_claim", "lifecycle_result"}),
                ),
                "reno": PrincipalConfig(
                    "reno",
                    "worker",
                    token_digest("reno-secret"),
                    frozenset({"conversation_recent"}),
                ),
            },
            cursor_secret=b"c" * 32,
            runtime_hmac_secret=b"r" * 32,
            transport_hmac_secret=b"t" * 32,
        )
        self.service = BrainService(self.settings)
        self.api = TransportAPI(self.service)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def envelope(
        self,
        *,
        message_id: str = "message-1",
        event_id: str | None = None,
        body: str = "hello",
        contact_key: str | None = "provided",
        transport_kind: str = "ordinary_inbound",
        external: dict[str, object] | None = None,
        display_name: str | None = None,
    ) -> dict[str, object]:
        event = event_id or self.runtime_ids.event_id("observer-a", message_id)
        payload: dict[str, object] = {
            "event_id": event,
            "observer_device_id": "observer-a",
            "received_at": 1000.0,
            "message_timestamp": 999.0,
            "remote_jid_hmac": self.runtime_ids.jid_hmac(f"{self.LID}@lid"),
            "body_hmac": self.runtime_ids.body_hmac(body),
            "body_length": len(body),
            "native_type": "extendedTextMessage",
            "transport_kind": transport_kind,
        }
        if contact_key == "provided":
            payload["contact_key"] = self.runtime_ids.contact_key(self.PHONE)
        elif contact_key is not None:
            payload["contact_key"] = contact_key
        if display_name is not None:
            payload["display_name"] = display_name
        if external is not None:
            payload["external_ad_reply"] = external
        return payload

    @staticmethod
    def request(
        payload: object,
        *,
        token: str = "observer-secret",
        declared_length: int | str | None = None,
        chunks: list[bytes] | None = None,
        method: str = "POST",
    ) -> Request:
        if chunks is None:
            if isinstance(payload, bytes):
                chunks = [payload]
            else:
                chunks = [json.dumps(payload, ensure_ascii=False).encode("utf-8")]
        index = 0

        async def receive() -> dict[str, object]:
            nonlocal index
            if index >= len(chunks):
                return {"type": "http.disconnect"}
            chunk = chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks),
            }

        headers = [
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
        ]
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        scope = {
            "type": "http",
            "method": method,
            "path": "/internal/transport/events",
            "headers": headers,
        }
        return Request(scope, receive)

    def post(
        self,
        payload: object,
        *,
        token: str = "observer-secret",
        declared_length: int | str | None = None,
        chunks: list[bytes] | None = None,
        method: str = "POST",
    ) -> SimpleNamespace:
        response = asyncio.run(
            self.api.events(
                self.request(
                    payload,
                    token=token,
                    declared_length=declared_length,
                    chunks=chunks,
                    method=method,
                )
            )
        )
        return SimpleNamespace(
            status_code=response.status_code,
            text=response.body.decode("utf-8"),
            json=lambda: json.loads(response.body),
        )

    def rows(self, table: str) -> list[sqlite3.Row]:
        return self.service.runtime.read(
            lambda conn: conn.execute(f"SELECT * FROM {table}").fetchall()
        )

    def ctwa(
        self, *, show: bool = False, auto_reply: bool = False
    ) -> dict[str, object]:
        return {
            "source_type": "ad",
            "source_app": "instagram",
            "source_id_present": True,
            "source_id_length": 12,
            "source_id_hmac": self.runtime_ids.opaque_hmac("source-id"),
            "source_url_hostname": "instagram.com",
            "source_url_length": 28,
            "source_url_hmac": self.runtime_ids.opaque_hmac("https://instagram.com/a"),
            "ctwa_clid_present": True,
            "ctwa_clid_length": 16,
            "ctwa_clid_hmac": self.runtime_ids.opaque_hmac("ctwa-clid"),
            "show_ad_attribution": show,
            "click_to_whatsapp_call": True,
            "contains_auto_reply": auto_reply,
        }

    def test_safe_ordinary_event_persists_once(self) -> None:
        response = self.post(self.envelope())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["duplicate"])
        self.assertEqual(len(self.rows("transport_events")), 1)

    def test_historical_ctwa_is_persisted_as_candidate(self) -> None:
        response = self.post(
            self.envelope(
                message_id="ctwa-1",
                transport_kind="ctwa_candidate",
                external=self.ctwa(),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.rows("transport_events")[0]["transport_kind"], "ctwa_candidate"
        )

    def test_show_ad_attribution_is_not_required_for_ctwa(self) -> None:
        response = self.post(
            self.envelope(
                message_id="ctwa-2",
                transport_kind="ctwa_candidate",
                external=self.ctwa(show=False),
            )
        )
        self.assertEqual(response.status_code, 200)

    def test_contains_auto_reply_does_not_create_human_semantics(self) -> None:
        response = self.post(
            self.envelope(
                message_id="ctwa-3",
                transport_kind="ctwa_candidate",
                external=self.ctwa(auto_reply=False),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("human", self.rows("transport_events")[0]["transport_kind"])

    def test_transport_kind_mismatch_is_rejected_without_persistence(self) -> None:
        response = self.post(
            self.envelope(
                transport_kind="ordinary_inbound",
                external=self.ctwa(),
            )
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_forbidden_raw_top_level_fields_are_rejected(self) -> None:
        for field in (
            "body",
            "remote_jid",
            "message_id",
            "sourceId",
            "ctwaClid",
            "sourceUrl",
            "contextInfo",
            "externalAdReply",
            "payload",
            "raw",
            "thumbnail",
        ):
            with self.subTest(field=field):
                payload = self.envelope()
                payload[field] = "raw"
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_forbidden_raw_nested_fields_are_rejected(self) -> None:
        for field in ("sourceId", "ctwaClid", "sourceUrl"):
            with self.subTest(field=field):
                payload = self.envelope(external={field: "raw"})
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)

    def test_unknown_fields_are_rejected(self) -> None:
        payload = self.envelope()
        payload["unknown"] = True
        self.assertEqual(self.post(payload).status_code, 400)

    def test_invalid_event_id_and_hmac_lengths_are_rejected(self) -> None:
        for payload in (
            self.envelope(event_id="not-an-event"),
            {**self.envelope(), "body_hmac": "00"},
            {**self.envelope(), "remote_jid_hmac": "zz" * 32},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)

    def test_bool_is_not_an_integer_and_timestamps_must_be_finite(self) -> None:
        for payload in (
            {**self.envelope(), "body_length": True},
            {**self.envelope(), "received_at": math.inf},
            {**self.envelope(), "message_timestamp": math.nan},
            {
                **self.envelope(external=self.ctwa()),
                "external_ad_reply": {
                    **self.ctwa(),
                    "source_id_present": None,
                },
            },
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)

    def test_oversized_declared_length_is_rejected_before_reading(self) -> None:
        response = self.post(self.envelope(), declared_length=16_385)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_invalid_declared_length_is_rejected(self) -> None:
        self.assertEqual(
            self.post(self.envelope(), declared_length="nope").status_code, 400
        )

    def test_oversized_streamed_body_is_rejected(self) -> None:
        chunks = [b"{" + b"a" * 16_384 + b"}"]
        response = self.post(b"ignored", chunks=chunks)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_invalid_json_is_rejected(self) -> None:
        self.assertEqual(self.post(b"{not-json").status_code, 400)

    def test_non_dict_json_is_rejected(self) -> None:
        self.assertEqual(self.post(["not", "an", "object"]).status_code, 400)

    def test_service_auth_rejects_worker_gateway_writer_and_invalid_tokens(
        self,
    ) -> None:
        for token in ("reno-secret", "gateway-secret", "writer-secret", "invalid"):
            with self.subTest(token=token):
                response = self.post(self.envelope(), token=token)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.rows("transport_events"), [])

    def test_authentication_happens_before_body_read(self) -> None:
        body_read = False

        async def receive() -> dict[str, object]:
            nonlocal body_read
            body_read = True
            raise AssertionError("body must not be read before authentication")

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/transport/events",
                "headers": [(b"authorization", b"Bearer invalid")],
            },
            receive,
        )
        response = asyncio.run(self.api.events(request))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(body_read)

    def test_identity_mapping_missing_is_retryable_without_persistence(self) -> None:
        self.observer_dir.joinpath(f"lid-mapping-{self.PHONE}.json").unlink()
        response = self.post(self.envelope())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.rows("transport_events"), [])

    def test_identity_mapping_ambiguous_is_retryable_without_persistence(self) -> None:
        (self.observer_dir / "lid-mapping-19999999998.json").write_text(
            json.dumps(self.LID), encoding="utf-8"
        )
        response = self.post(self.envelope())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.rows("transport_events"), [])

    def test_divergent_contact_key_fails_closed(self) -> None:
        response = self.post(
            self.envelope(contact_key=self.runtime_ids.contact_key("19999999998"))
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.rows("transport_events"), [])

    def test_missing_contact_key_uses_only_proven_derived_key(self) -> None:
        response = self.post(self.envelope(contact_key=None))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.rows("transport_events")[0]["contact_key"],
            self.runtime_ids.contact_key(self.PHONE),
        )

    def test_duplicate_identical_event_is_idempotent(self) -> None:
        payload = self.envelope()
        first = self.post(payload)
        second = self.post(payload)
        self.assertEqual(first.status_code, second.status_code, 200)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(len(self.rows("transport_events")), 1)

    def test_conflicting_duplicate_is_rejected_and_original_intact(self) -> None:
        first_payload = self.envelope()
        self.assertEqual(self.post(first_payload).status_code, 200)
        conflict = self.envelope(body="different")
        response = self.post(conflict)
        self.assertEqual(response.status_code, 400)
        rows = self.rows("transport_events")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body_hmac"], first_payload["body_hmac"])

    def test_display_name_is_sanitized_and_ephemeral(self) -> None:
        response = self.post(self.envelope(display_name="Jo\não\x00 Silva"))
        self.assertEqual(response.status_code, 200)
        event = self.rows("transport_events")[0]
        ephemera = self.rows("contact_ephemera")[0]
        self.assertNotIn("display_name", event.keys())
        self.assertEqual(ephemera["display_name"], "João Silva")
        self.assertEqual(
            ephemera["display_name_hmac"], self.runtime_ids.opaque_hmac("João Silva")
        )

    def test_display_name_is_bounded_and_expiry_uses_settings(self) -> None:
        name = "x" * 200
        self.assertEqual(self.post(self.envelope(display_name=name)).status_code, 200)
        ephemera = self.rows("contact_ephemera")[0]
        self.assertEqual(len(ephemera["display_name"]), 160)
        self.assertAlmostEqual(
            ephemera["expires_at"] - ephemera["created_at"], 24 * 3600
        )

    def test_missing_display_name_does_not_create_ephemera(self) -> None:
        self.assertEqual(self.post(self.envelope()).status_code, 200)
        self.assertEqual(self.rows("contact_ephemera"), [])

    def test_ephemera_failure_rolls_back_event(self) -> None:
        original_write = self.service.runtime.write

        def failing_write(callback):
            def failing_callback(conn):
                callback(conn)
                raise sqlite3.OperationalError("ephemera failure")

            return original_write(failing_callback)

        self.service.runtime.write = failing_write  # type: ignore[method-assign]
        response = self.post(self.envelope(display_name="Name"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.rows("transport_events"), [])
        self.assertEqual(self.rows("contact_ephemera"), [])

    def test_endpoint_does_not_accept_get(self) -> None:
        response = self.post(self.envelope(), method="GET")
        self.assertEqual(response.status_code, 405)

    def test_route_is_private_and_not_model_visible(self) -> None:
        app = BrainMCPServer(self.service).app()
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        self.assertIn("/internal/transport/events", paths)
        self.assertNotIn("transport_ingest", {tool.name for tool in _tools()})


if __name__ == "__main__":
    unittest.main()
