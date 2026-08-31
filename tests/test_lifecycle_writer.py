from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.famachat_client import FamaChatClient
from brain.lifecycle_api import ClaimedEffect
from brain.lifecycle_writer import LifecycleWriter

_DEFAULT = object()

BRAIN_PHONE = "5534999772714"
FAMACHAT_PHONE = "553499772714"
FINGERPRINT = "4da4e773feb862db22b273d159fc9ba456549df0052e52d2d90d00a17f34fd11"

CLIENT_BODY = {
    "id": 12800,
    "phone": FAMACHAT_PHONE,
    "brokerId": 35,
    "status": "Sem Atendimento",
    "source": "Facebook Ads",
}


class FakeClaims:
    def __init__(self, claim: ClaimedEffect | None) -> None:
        self._claim = claim
        self.reported: list[tuple[str, str, str]] = []

    def claim(self) -> ClaimedEffect | None:
        claim, self._claim = self._claim, None
        return claim

    def report(self, effect_id: str, lease_token: str, result: str) -> bool:
        self.reported.append((effect_id, lease_token, result))
        return True


class WriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proof_path = Path(self.temp_dir.name) / "proof.json"
        self.body = dict(CLIENT_BODY)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def transport(self, tool: str, arguments: dict) -> dict:
        self.calls.append(tool)
        return {"status": 200, "body": self.body}

    @staticmethod
    def claim(**kwargs) -> ClaimedEffect:
        options = {
            "effect_id": "fx_1",
            "lease_token": "t" * 32,
            "client_id": 12800,
            "expected_status": "Sem Atendimento",
            "target_status": "Não Respondeu",
            "cause": "first_t1_send_success",
            "mode": "shadow",
            "expected_phone_e164": BRAIN_PHONE,
        }
        options.update(kwargs)
        return ClaimedEffect(**options)

    def write_proof(self, **overrides) -> None:
        proof = {"verdict": "PASS", "schema_fingerprint": FINGERPRINT}
        proof.update(overrides)
        self.proof_path.write_text(json.dumps(proof), encoding="utf-8")

    def writer(
        self, claim: ClaimedEffect | None = _DEFAULT, **kwargs
    ) -> LifecycleWriter:
        self.claims = FakeClaims(self.claim() if claim is _DEFAULT else claim)
        options = {
            "write_enabled": False,
            "proof_path": self.proof_path,
            "schema_fingerprint": FINGERPRINT,
        }
        options.update(kwargs)
        return LifecycleWriter(self.claims, FamaChatClient(self.transport), **options)

    def assert_no_mutation(self) -> None:
        for tool in self.calls:
            self.assertTrue(tool.startswith("fc_get_"), f"mutating call: {tool}")

    # ------------------------------------------------------------------

    def test_dry_run_validates_everything_and_writes_nothing(self) -> None:
        outcome = self.writer().run_once()

        self.assertEqual(outcome.outcome, "would_apply")
        self.assertEqual(outcome.reason, "write_disabled_locally")
        self.assert_no_mutation()

    def test_would_apply_is_not_reported_back_as_a_result(self) -> None:
        """Dry run leaves the effect claimed; it is not a settled outcome."""
        writer = self.writer()

        writer.run_once()

        self.assertEqual(self.claims.reported, [])

    def test_nothing_to_do_is_idle(self) -> None:
        outcome = self.writer(claim=None).run_once()

        self.assertEqual(outcome.outcome, "idle")
        self.assertEqual(self.calls, [])

    def test_the_ninth_digit_difference_still_counts_as_the_same_lead(self) -> None:
        outcome = self.writer().run_once()

        self.assertEqual(outcome.outcome, "would_apply")

    # ------------------------------------------------------------------
    # Refusals, all reported and none mutating

    def test_a_different_phone_is_a_conflict(self) -> None:
        self.body["phone"] = "5511988887777"

        outcome = self.writer().run_once()

        self.assertEqual(outcome.outcome, "conflict")
        self.assertEqual(outcome.reason, "phone_mismatch")
        self.assertEqual(self.claims.reported[0][2], "conflict")
        self.assert_no_mutation()

    def test_another_broker_is_a_conflict(self) -> None:
        self.body["brokerId"] = 41

        outcome = self.writer().run_once()

        self.assertEqual(outcome.reason, "broker_mismatch")
        self.assert_no_mutation()

    def test_another_source_is_a_conflict(self) -> None:
        self.body["source"] = "Indicação"

        outcome = self.writer().run_once()

        self.assertEqual(outcome.reason, "source_mismatch")
        self.assert_no_mutation()

    def test_a_status_someone_else_moved_is_a_conflict(self) -> None:
        """The case the whole design exists for: never walk back a human."""
        self.body["status"] = "Em Atendimento"

        outcome = self.writer(
            claim=self.claim(target_status="Não Respondeu")
        ).run_once()

        self.assertEqual(outcome.outcome, "conflict")
        self.assertEqual(outcome.reason, "unexpected_current_status")
        self.assert_no_mutation()

    def test_a_record_already_at_the_target_is_already_applied(self) -> None:
        self.body["status"] = "Não Respondeu"

        outcome = self.writer().run_once()

        self.assertEqual(outcome.outcome, "already_applied")
        self.assertEqual(self.claims.reported[0][2], "already_applied")
        self.assert_no_mutation()

    def test_a_missing_client_is_a_conflict(self) -> None:
        self.transport = lambda tool, args: {"status": 404, "body": {}}

        outcome = self.writer().run_once()

        self.assertEqual(outcome.reason, "client_not_found")

    def test_famachat_unavailable_is_retryable(self) -> None:
        self.transport = lambda tool, args: {"status": 503, "body": {}}

        outcome = self.writer().run_once()

        self.assertEqual(outcome.outcome, "retryable")
        self.assertEqual(self.claims.reported[0][2], "retryable")

    def test_an_unauthorised_transition_is_refused_even_if_claimed(self) -> None:
        self.body["status"] = "Em Atendimento"

        outcome = self.writer(
            claim=self.claim(
                expected_status="Em Atendimento", target_status="Sem Atendimento"
            )
        ).run_once()

        self.assertEqual(outcome.reason, "unauthorised_transition")
        self.assert_no_mutation()

    # ------------------------------------------------------------------
    # The three agreements write mode requires

    def test_write_needs_the_local_switch(self) -> None:
        self.write_proof()

        allowed, reason = self.writer(
            claim=self.claim(mode="write"), write_enabled=False
        ).may_write(self.claim(mode="write"))

        self.assertFalse(allowed)
        self.assertEqual(reason, "write_disabled_locally")

    def test_write_needs_brain_to_agree(self) -> None:
        self.write_proof()

        allowed, reason = self.writer(write_enabled=True).may_write(
            self.claim(mode="shadow")
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "brain_mode_is_not_write")

    def test_write_needs_a_passing_proof(self) -> None:
        writer = self.writer(write_enabled=True)
        claim = self.claim(mode="write")

        allowed, reason = writer.may_write(claim)
        self.assertEqual(reason, "no_conditional_write_proof")

        self.write_proof(verdict="FAIL")
        allowed, reason = writer.may_write(claim)
        self.assertFalse(allowed)
        self.assertEqual(reason, "conditional_write_proof_did_not_pass")

    def test_write_needs_the_proof_to_describe_this_server(self) -> None:
        """A changed schema means the proof no longer describes reality."""
        self.write_proof(schema_fingerprint="0" * 64)

        allowed, reason = self.writer(write_enabled=True).may_write(
            self.claim(mode="write")
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "schema_fingerprint_mismatch")

    def test_all_three_agreements_together_allow_the_write(self) -> None:
        self.write_proof()

        allowed, reason = self.writer(write_enabled=True).may_write(
            self.claim(mode="write")
        )

        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_the_writer_imports_no_model_or_provider(self) -> None:
        """Spec 13: the writer is deterministic code, never a model.

        Scans imports rather than prose, so the docstring may say the rule out
        loud without the test tripping on its own words.
        """
        import ast

        source = (
            Path(__file__).resolve().parents[1] / "src/brain/lifecycle_writer.py"
        ).read_text(encoding="utf-8")
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        for name in imported:
            root = name.split(".")[0].lower()
            self.assertNotIn(
                root,
                {"openai", "anthropic", "litellm", "transformers", "httpx", "requests"},
                f"writer imports {name}",
            )


if __name__ == "__main__":
    unittest.main()
