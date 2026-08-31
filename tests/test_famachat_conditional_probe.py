from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load("probe_famachat_conditional_status")
capture = _load("capture_famachat_writer_schema")


def schema(patch_body: dict) -> dict:
    return {
        "fingerprint": "f" * 64,
        "tools": [
            {
                "name": "fc_get_clientes_by_id",
                "description": "GET /api/clientes/:id",
                "input_schema": {"properties": {"id": {}}},
            },
            {
                "name": "fc_patch_clientes_by_id",
                "description": "PATCH /api/clientes/:id",
                "input_schema": {"properties": {"id": {}, "body": patch_body}},
            },
        ],
    }


class InspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "schema.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, payload: dict) -> Path:
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return self.path

    def test_a_free_form_body_is_no_precondition(self) -> None:
        """The shape FamaChat had before the change: any key accepted, none meant."""
        path = self.write(schema({"type": "object", "additionalProperties": {}}))

        self.assertEqual(probe.inspect(path)["verdict"], probe.NO_ATOMIC_PRECONDITION)

    def test_a_declared_expected_status_is_a_candidate(self) -> None:
        path = self.write(
            schema(
                {
                    "type": "object",
                    "additionalProperties": {},
                    "properties": {"expectedStatus": {"type": "string"}},
                }
            )
        )

        result = probe.inspect(path)

        self.assertEqual(result["verdict"], probe.CANDIDATE)
        self.assertEqual(result["field"], "expectedStatus")
        self.assertEqual(result["strategy"], "expected_status_in_body")

    def test_other_conditional_shapes_are_recognised(self) -> None:
        for field in ("expected_status", "ifMatch", "version"):
            with self.subTest(field=field):
                path = self.write(schema({"type": "object", "properties": {field: {}}}))
                result = probe.inspect(path)
                self.assertEqual(result["verdict"], probe.CANDIDATE)
                self.assertEqual(result["field"], field)

    def test_a_missing_tool_is_unavailable(self) -> None:
        payload = schema({"properties": {"expectedStatus": {}}})
        payload["tools"] = [payload["tools"][0]]
        path = self.write(payload)

        self.assertEqual(probe.inspect(path)["verdict"], probe.UNAVAILABLE)

    def test_an_unreadable_schema_is_unavailable(self) -> None:
        self.path.write_text("not json", encoding="utf-8")

        self.assertEqual(probe.inspect(self.path)["verdict"], probe.UNAVAILABLE)

    def test_a_candidate_says_it_is_not_yet_proof(self) -> None:
        """Premise P15: a server may declare the field and ignore it."""
        path = self.write(schema({"properties": {"expectedStatus": {}}}))

        self.assertIn("behaviour", probe.inspect(path)["note"])

    def test_inspection_never_calls_famachat(self) -> None:
        source = (SCRIPTS / "probe_famachat_conditional_status.py").read_text(
            encoding="utf-8"
        )
        body = source[source.index("def inspect(") : source.index("async def _call(")]
        for forbidden in ("call_tool", "http", "session", "asyncio"):
            self.assertNotIn(forbidden, body)


class CaptureTests(unittest.TestCase):
    def test_the_fingerprint_covers_the_schema_not_the_order(self) -> None:
        tools = schema({"properties": {"expectedStatus": {}}})["tools"]
        self.assertEqual(capture.fingerprint(tools), capture.fingerprint(list(tools)))

    def test_the_fingerprint_changes_when_the_schema_changes(self) -> None:
        """This is what the write gate compares before starting."""
        before = schema({"type": "object", "additionalProperties": {}})["tools"]
        after = schema({"properties": {"expectedStatus": {}}})["tools"]

        self.assertNotEqual(capture.fingerprint(before), capture.fingerprint(after))

    def test_credentials_are_redacted_from_errors(self) -> None:
        redacted = capture.redact("failed with Authorization: Bearer sk-live-secret")
        self.assertNotIn("sk-live-secret", redacted)

    def test_the_checked_in_fixture_matches_its_own_fingerprint(self) -> None:
        """A hand-edited fixture would silently weaken the gate."""
        path = Path(__file__).resolve().parents[1] / (
            "tests/fixtures/famachat-writer-tools.json"
        )
        captured = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            capture.fingerprint(captured["tools"]), captured["fingerprint"]
        )

    def test_the_checked_in_fixture_carries_no_credential(self) -> None:
        path = Path(__file__).resolve().parents[1] / (
            "tests/fixtures/famachat-writer-tools.json"
        )
        body = path.read_text(encoding="utf-8")

        self.assertEqual(capture.redact(body), body)

    def test_the_checked_in_fixture_declares_the_proven_field(self) -> None:
        path = Path(__file__).resolve().parents[1] / (
            "tests/fixtures/famachat-writer-tools.json"
        )
        captured = json.loads(path.read_text(encoding="utf-8"))
        patch = next(
            tool
            for tool in captured["tools"]
            if tool["name"] == "fc_patch_clientes_by_id"
        )

        declared = patch["input_schema"]["properties"]["body"].get("properties") or {}
        self.assertIn("expectedStatus", declared)


if __name__ == "__main__":
    unittest.main()
