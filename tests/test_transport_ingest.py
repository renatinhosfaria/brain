from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        self.runtime_ids = RuntimeIds(b"t" * 32)
        self.settings = BrainSettings(
            state_db=self.state_path,
            kanban_db=self.kanban_path,
            runtime_db=self.runtime_path,
            observer_session_dir=self.observer_dir,
            observer_device_ids=("observer-a",),
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-secret"),
                    frozenset({"conversation_context"}),
                ),
                "observer": PrincipalConfig(
                    "observer",
                    "service",
                    token_digest("observer-secret"),
                    frozenset({"transport_ingest"}),
                ),
                "reno": PrincipalConfig(
                    "reno",
                    "worker",
                    token_digest("reno-secret"),
                    frozenset({"conversation_recent"}),
                ),
            },
            cursor_secret=b"c" * 32,
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
        observer_event_version: int | None = None,
        raw: object | None = None,
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
        if observer_event_version is not None:
            payload["observer_event_version"] = observer_event_version
        if raw is not None:
            payload["external_ad_reply_raw"] = raw
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

    def raw_ctwa(self) -> dict[str, object]:
        return {
            "sourceType": "ad",
            "sourceApp": "instagram",
            "sourceId": "source-id",
            "sourceUrl": "https://instagram.com/ad?utm=ctwa",
            "ctwaClid": "ctwa-clid",
            "showAdAttribution": False,
            "clickToWhatsappCall": True,
            "containsAutoReply": False,
            "thumbnail": {
                "$type": "bytes",
                "encoding": "base64",
                "data": "cmF3LXRodW1ibmFpbA==",
            },
            "unknownNested": ["preserve", {"unexpected": True}],
        }

    def normalized_ctwa_for_raw(self) -> dict[str, object]:
        url = "https://instagram.com/ad?utm=ctwa"
        return {
            "source_type": "ad",
            "source_app": "instagram",
            "source_id_present": True,
            "source_id_length": 9,
            "source_id_hmac": self.runtime_ids.opaque_hmac("source-id"),
            "source_url_hostname": "instagram.com",
            "source_url_length": 33,
            "source_url_hmac": self.runtime_ids.opaque_hmac(url),
            "ctwa_clid_present": True,
            "ctwa_clid_length": 9,
            "ctwa_clid_hmac": self.runtime_ids.opaque_hmac("ctwa-clid"),
            "show_ad_attribution": False,
            "click_to_whatsapp_call": True,
            "contains_auto_reply": False,
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

    def test_v2_ctwa_persists_the_exact_canonical_raw_capture(self) -> None:
        raw = self.raw_ctwa()
        response = self.post(
            self.envelope(
                message_id="v2-raw",
                transport_kind="ctwa_candidate",
                external=self.normalized_ctwa_for_raw(),
                observer_event_version=2,
                raw=raw,
            )
        )

        self.assertEqual(response.status_code, 200)
        stored = self.rows("transport_events")[0]["external_ad_reply_raw_json"]
        self.assertEqual(json.loads(stored), raw)

    def test_v2_rejects_integer_valued_unsafe_raw_floats(self) -> None:
        for index, value in enumerate((9007199254740992.0, 1e20)):
            raw = self.raw_ctwa()
            raw["futureNumber"] = value
            response = self.post(
                self.envelope(
                    message_id=f"unsafe-float-{index}",
                    transport_kind="ctwa_candidate",
                    external=self.normalized_ctwa_for_raw(),
                    observer_event_version=2,
                    raw=raw,
                )
            )

            with self.subTest(value=value):
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_v2_accepts_raw_attribution_at_the_exact_observer_byte_limit(
        self,
    ) -> None:
        maximum = 4 * 1024 * 1024
        empty = '{"number":1e-7,"padding":""}'
        raw = {
            "number": 1e-7,
            "padding": "x" * (maximum - len(empty.encode("utf-8"))),
        }
        external = {
            "source_id_present": False,
            "ctwa_clid_present": False,
        }

        response = self.post(
            self.envelope(
                message_id="exact-raw-limit",
                transport_kind="ordinary_inbound",
                external=external,
                observer_event_version=2,
                raw=raw,
            )
        )

        self.assertEqual(response.status_code, 200)
        stored = self.rows("transport_events")[0]["external_ad_reply_raw_json"]
        self.assertEqual(len(stored.encode("utf-8")), maximum)
        self.assertIn('"number":1e-7', stored)

    def test_v2_cross_checks_whatwg_idna_and_port_semantics(self) -> None:
        valid_url = "https://bücher.example:8443/path"
        invalid_url = "https://example.test:99999/path"
        base_external = {
            "source_id_present": False,
            "ctwa_clid_present": False,
        }
        valid_external = {
            **base_external,
            "source_url_hostname": "xn--bcher-kva.example",
            "source_url_length": len(valid_url),
            "source_url_hmac": self.runtime_ids.opaque_hmac(valid_url),
        }

        valid = self.post(
            self.envelope(
                message_id="idn-valid-port",
                transport_kind="ordinary_inbound",
                external=valid_external,
                observer_event_version=2,
                raw={"sourceUrl": valid_url},
            )
        )
        invalid = self.post(
            self.envelope(
                message_id="invalid-port",
                transport_kind="ordinary_inbound",
                external=base_external,
                observer_event_version=2,
                raw={"sourceUrl": invalid_url},
            )
        )

        self.assertEqual(valid.status_code, 200)
        self.assertEqual(invalid.status_code, 200)

    def test_v2_raw_replay_is_idempotent(self) -> None:
        payload = self.envelope(
            message_id="v2-replay",
            transport_kind="ctwa_candidate",
            external=self.normalized_ctwa_for_raw(),
            observer_event_version=2,
            raw=self.raw_ctwa(),
        )

        first = self.post(payload)
        second = self.post(payload)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(len(self.rows("transport_events")), 1)

    def test_changed_v2_raw_conflicts_and_preserves_the_original(self) -> None:
        original = self.envelope(
            message_id="v2-conflict",
            transport_kind="ctwa_candidate",
            external=self.normalized_ctwa_for_raw(),
            observer_event_version=2,
            raw=self.raw_ctwa(),
        )
        self.assertEqual(self.post(original).status_code, 200)
        changed_raw = self.raw_ctwa()
        changed_raw["unknownNested"] = ["changed"]
        conflict = {**original, "external_ad_reply_raw": changed_raw}

        self.assertEqual(self.post(conflict).status_code, 400)
        stored = self.rows("transport_events")
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            json.loads(stored[0]["external_ad_reply_raw_json"]), original["external_ad_reply_raw"]
        )

    def test_v2_raw_must_match_normalized_external_metadata(self) -> None:
        mismatched = self.normalized_ctwa_for_raw()
        mismatched["source_id_length"] = 8

        response = self.post(
            self.envelope(
                message_id="v2-mismatch",
                transport_kind="ctwa_candidate",
                external=mismatched,
                observer_event_version=2,
                raw=self.raw_ctwa(),
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_v2_ctwa_requires_its_raw_capture(self) -> None:
        response = self.post(
            self.envelope(
                message_id="v2-missing-raw",
                transport_kind="ctwa_candidate",
                external=self.normalized_ctwa_for_raw(),
                observer_event_version=2,
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_only_integer_version_2_marks_a_v2_envelope(self) -> None:
        response = self.post(
            self.envelope(observer_event_version=2.0)  # type: ignore[arg-type]
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_legacy_v1_ctwa_remains_accepted_without_raw_capture(self) -> None:
        response = self.post(
            self.envelope(
                message_id="legacy-v1",
                transport_kind="ctwa_candidate",
                external=self.ctwa(),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.rows("transport_events")[0]["external_ad_reply_raw_json"])

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
        response = self.post(
            self.envelope(), declared_length=5 * 1024 * 1024 + 1
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_invalid_declared_length_is_rejected(self) -> None:
        self.assertEqual(
            self.post(self.envelope(), declared_length="nope").status_code, 400
        )

    def test_oversized_streamed_body_is_rejected(self) -> None:
        chunks = [b"{" + b"a" * (5 * 1024 * 1024) + b"}"]
        response = self.post(b"ignored", chunks=chunks)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_body_over_five_mebibytes_is_rejected_before_json_parsing(self) -> None:
        chunks = [b"x" * (5 * 1024 * 1024 + 1)]
        with patch(
            "brain.transport_api.json.loads",
            side_effect=AssertionError("oversized body must not be parsed"),
        ):
            response = self.post(b"ignored", chunks=chunks)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_invalid_json_is_rejected(self) -> None:
        self.assertEqual(self.post(b"{not-json").status_code, 400)

    def test_deeply_nested_json_is_rejected_at_the_http_boundary(self) -> None:
        body = b"[" * 1_100 + b"0" + b"]" * 1_100

        response = self.post(body)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_json_integer_past_python_limit_is_rejected_at_the_http_boundary(self) -> None:
        body = b'{"value":' + b"1" * 5_000 + b"}"

        response = self.post(body)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows("transport_events"), [])

    def test_non_dict_json_is_rejected(self) -> None:
        self.assertEqual(self.post(["not", "an", "object"]).status_code, 400)

    def test_service_auth_rejects_worker_gateway_and_invalid_tokens(
        self,
    ) -> None:
        for token in ("reno-secret", "gateway-secret", "invalid"):
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

    def test_ingestion_stays_durable_when_re_evaluation_fails(self) -> None:
        """Transport ACK must not depend on correlation succeeding (Task 3)."""

        def explode(_contact_key: str) -> int:
            raise RuntimeError("correlation is broken")

        self.service.transport_service.on_contact_observed = explode

        response = self.post(self.envelope())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows("transport_events")), 1)

    def test_route_is_private_and_not_model_visible(self) -> None:
        app = BrainMCPServer(self.service).app()
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        self.assertIn("/internal/transport/events", paths)
        self.assertNotIn("transport_ingest", {tool.name for tool in _tools()})


if __name__ == "__main__":
    unittest.main()


class RetentionTests(TransportIngestTests):
    """Section 19, enforced on the path that creates the governed data."""

    def rows_of(self, table: str) -> list:
        return self.rows(table)

    def test_first_ingestion_enforces_policy_immediately(self) -> None:
        self.post(self.envelope(display_name="Name"))
        service = self.service.transport_service

        self.assertGreater(service._retention_ran_at, 0.0)

    def test_expired_display_name_is_deleted_not_merely_hidden(self) -> None:
        self.post(self.envelope(display_name="Maria Silva"))
        self.service.runtime.write(
            lambda conn: conn.execute(
                "UPDATE contact_ephemera SET expires_at = 1.0"
            )
        )
        self.service.transport_service._retention_ran_at = 0.0

        self.post(self.envelope(message_id="later", body="oi de novo"))

        stored = self.service.runtime.read(
            lambda conn: conn.execute(
                "SELECT display_name FROM contact_ephemera"
            ).fetchone()[0]
        )
        self.assertIsNone(stored)

    def test_a_live_display_name_survives(self) -> None:
        self.post(self.envelope(display_name="Maria Silva"))
        self.service.transport_service._retention_ran_at = 0.0

        self.post(self.envelope(message_id="later", body="oi de novo"))

        stored = self.service.runtime.read(
            lambda conn: conn.execute(
                "SELECT display_name FROM contact_ephemera"
            ).fetchone()[0]
        )
        self.assertEqual(stored, "Maria Silva")

    def test_transport_events_past_the_window_are_purged(self) -> None:
        self.post(self.envelope(message_id="old", body="antigo"))
        cutoff_days = self.service.settings.transport_retention_days
        self.service.runtime.write(
            lambda conn: conn.execute(
                "UPDATE transport_events SET created_at = ?",
                (time.time() - (cutoff_days + 1) * 86_400,),
            )
        )
        self.service.transport_service._retention_ran_at = 0.0

        self.post(self.envelope(message_id="new", body="recente"))

        remaining = self.service.runtime.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM transport_events"
            ).fetchone()[0]
        )
        self.assertEqual(remaining, 1)

    def test_retention_deletes_events_that_contain_raw_attribution(self) -> None:
        self.post(
            self.envelope(
                message_id="old-raw",
                transport_kind="ctwa_candidate",
                external=self.normalized_ctwa_for_raw(),
                observer_event_version=2,
                raw=self.raw_ctwa(),
            )
        )
        cutoff_days = self.service.settings.transport_retention_days
        self.service.runtime.write(
            lambda conn: conn.execute(
                "UPDATE transport_events SET created_at = ?",
                (time.time() - (cutoff_days + 1) * 86_400,),
            )
        )
        self.service.transport_service._retention_ran_at = 0.0

        self.post(self.envelope(message_id="new", body="recente"))

        rows = self.rows("transport_events")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["external_ad_reply_raw_json"])

    def test_retention_is_throttled_between_passes(self) -> None:
        self.post(self.envelope(message_id="one", body="um"))
        first = self.service.transport_service._retention_ran_at

        self.post(self.envelope(message_id="two", body="dois"))

        self.assertEqual(self.service.transport_service._retention_ran_at, first)

    def test_a_failing_retention_pass_does_not_fail_ingestion(self) -> None:
        self.service.transport_service._retention_ran_at = 0.0
        original = self.service.runtime.write

        def write(callback):
            if callback.__name__ == "purge":
                raise sqlite3.OperationalError("retention exploded")
            return original(callback)

        self.service.runtime.write = write  # type: ignore[method-assign]
        response = self.post(self.envelope(message_id="durable", body="fica"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows("transport_events")), 1)


class RetentionRetryTests(TransportIngestTests):
    """The retention pass must fail alone, and be retried alone."""

    @staticmethod
    def _fail_only_the_purge(original, should_fail):
        """Break the purge without touching the durable write beside it.

        Failing whichever write comes first would break ingestion instead, and
        the assertions below would then pass for entirely the wrong reason.
        """

        def write(callback):
            if callback.__name__ == "purge" and should_fail():
                raise sqlite3.OperationalError("retention exploded")
            return original(callback)

        return write

    def test_a_failed_pass_is_retried_by_the_next_event(self) -> None:
        service = self.service.transport_service
        service._retention_ran_at = 0.0
        broken = {"on": True}
        self.service.runtime.write = self._fail_only_the_purge(  # type: ignore[method-assign]
            self.service.runtime.write, lambda: broken["on"]
        )

        first = self.post(self.envelope(message_id="one", body="um"))

        # The ingestion itself must be untouched, or this proves nothing.
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(self.rows("transport_events")), 1)
        self.assertEqual(service._retention_ran_at, 0.0)

        broken["on"] = False
        second = self.post(self.envelope(message_id="two", body="dois"))

        self.assertEqual(second.status_code, 200)
        self.assertGreater(service._retention_ran_at, 0.0)

    def test_a_failed_purge_still_leaves_the_event_durable(self) -> None:
        self.service.transport_service._retention_ran_at = 0.0
        self.service.runtime.write = self._fail_only_the_purge(  # type: ignore[method-assign]
            self.service.runtime.write, lambda: True
        )

        response = self.post(self.envelope(message_id="durable", body="fica"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows("transport_events")), 1)


class RetentionLoopTests(TransportIngestTests):
    """The periodic pass must do the work, not merely exist."""

    def expire_the_display_name(self) -> None:
        self.post(self.envelope(display_name="Maria Silva"))
        self.service.runtime.write(
            lambda conn: conn.execute("UPDATE contact_ephemera SET expires_at = 1.0")
        )
        self.service.transport_service._retention_ran_at = 0.0

    def stored_display_name(self):
        return self.service.runtime.read(
            lambda conn: conn.execute(
                "SELECT display_name FROM contact_ephemera"
            ).fetchone()[0]
        )

    def run_lifespan(self, seconds: float = 1.5) -> None:
        """Drive the real app lifespan with a short interval and wait.

        Nothing here calls apply_retention. If the periodic task is not
        started, or does not reach the purge, the display name survives and
        the assertion fails — which is the only way this proves anything.
        """
        from brain import mcp_server

        app = mcp_server.BrainMCPServer(self.service).app()

        async def window() -> None:
            async with app.router.lifespan_context(app):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if self.stored_display_name() is None:
                        return
                    await asyncio.sleep(0.02)

        with patch.object(mcp_server, "RETENTION_LOOP_SECONDS", 0.01):
            asyncio.run(window())

    def test_the_periodic_pass_purges_without_any_ingestion(self) -> None:
        self.expire_the_display_name()

        self.run_lifespan()

        self.assertIsNone(self.stored_display_name())

    def test_a_live_display_name_survives_the_periodic_pass(self) -> None:
        self.post(self.envelope(display_name="Maria Silva"))
        self.service.transport_service._retention_ran_at = 0.0

        self.run_lifespan(seconds=0.3)

        self.assertEqual(self.stored_display_name(), "Maria Silva")

    def test_the_loop_is_bound_to_the_app_lifespan(self) -> None:
        from brain.mcp_server import BrainMCPServer

        app = BrainMCPServer(self.service).app()
        source = app.router.lifespan_context.__wrapped__.__code__

        self.assertIn("_retention_loop", source.co_names)

    def test_the_wrapped_mcp_lifespan_still_runs(self) -> None:
        """Wrapping must not swallow the lifespan the MCP app owns.

        The spy is installed on the app the MCP library builds, before our
        wrapper captures it. Spying on the wrapper instead would only prove
        the wrapper calls itself.
        """
        from brain import mcp_server

        server = mcp_server.BrainMCPServer(self.service)
        build = server.server.streamable_http_app
        entered = {"mcp": False}

        def instrumented(**kwargs):
            application = build(**kwargs)
            mcp_lifespan = application.router.lifespan_context

            @contextlib.asynccontextmanager
            async def spy(app):
                entered["mcp"] = True
                async with mcp_lifespan(app):
                    yield

            application.router.lifespan_context = spy
            return application

        server.server.streamable_http_app = instrumented
        app = server.app()

        async def window() -> None:
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)

        asyncio.run(window())

        self.assertTrue(entered["mcp"])
