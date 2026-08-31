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

    # ------------------------------------------------------------------
    # With all three agreements, the proven strategy and its readback

    def enabled_writer(self, **kwargs):
        self.write_proof()
        return self.writer(claim=self.claim(mode="write"), write_enabled=True, **kwargs)

    def test_the_mutation_sends_exactly_the_proven_envelope(self) -> None:
        """The call shape must match the proof, not a variation of it."""
        sent: list[tuple[str, dict]] = []

        def transport(tool, arguments):
            sent.append((tool, arguments))
            if tool == "fc_patch_clientes_by_id":
                return {"status": 200, "body": {}}
            body = dict(self.body)
            if len(sent) > 2:
                body["status"] = "Não Respondeu"
            return {"status": 200, "body": body}

        self.transport = transport
        outcome = self.enabled_writer().run_once()

        self.assertEqual(outcome.outcome, "applied")
        patch = next(call for call in sent if call[0] == "fc_patch_clientes_by_id")
        self.assertEqual(
            patch[1],
            {
                "id": 12800,
                "body": {
                    "status": "Não Respondeu",
                    "expectedStatus": "Sem Atendimento",
                },
            },
        )

    def test_a_server_refusal_is_a_conflict_not_a_failure(self) -> None:
        """409 means a human moved the record. The protection worked."""

        def transport(tool, arguments):
            if tool == "fc_patch_clientes_by_id":
                return {
                    "status": 409,
                    "body": {"error": "Conflito", "currentStatus": "Em Atendimento"},
                }
            return {"status": 200, "body": self.body}

        self.transport = transport
        outcome = self.enabled_writer().run_once()

        self.assertEqual(outcome.outcome, "conflict")
        self.assertEqual(outcome.reason, "server_refused_stale_state")
        self.assertEqual(self.claims.reported[0][2], "conflict")

    def test_readback_disagreeing_with_a_200_is_not_trusted(self) -> None:
        """A 200 is the server's claim; the record is the evidence."""

        def transport(tool, arguments):
            if tool == "fc_patch_clientes_by_id":
                return {"status": 200, "body": {}}
            return {"status": 200, "body": self.body}  # never changes

        self.transport = transport
        outcome = self.enabled_writer().run_once()

        self.assertEqual(outcome.outcome, "retryable")
        self.assertEqual(outcome.reason, "readback_unchanged")

    def test_an_ambiguous_write_reads_before_deciding(self) -> None:
        """A timeout is not a failure: the change may already have landed."""
        calls: list[str] = []

        def transport(tool, arguments):
            calls.append(tool)
            if tool == "fc_patch_clientes_by_id":
                raise TimeoutError("connection reset")
            body = dict(self.body)
            if len(calls) > 1:
                body["status"] = "Não Respondeu"
            return {"status": 200, "body": body}

        self.transport = transport
        outcome = self.enabled_writer().run_once()

        self.assertEqual(outcome.outcome, "already_applied")
        self.assertEqual(calls[-1], "fc_get_clientes_by_id")

    def test_an_ambiguous_write_that_did_not_land_is_retryable(self) -> None:
        def transport(tool, arguments):
            if tool == "fc_patch_clientes_by_id":
                raise TimeoutError("connection reset")
            return {"status": 200, "body": self.body}

        self.transport = transport
        outcome = self.enabled_writer().run_once()

        self.assertEqual(outcome.outcome, "retryable")
        self.assertEqual(outcome.reason, "readback_unchanged")

    def test_an_ambiguous_write_with_an_unreadable_record_stays_retryable(self) -> None:
        """Validation read succeeds, the write is ambiguous, the readback fails."""
        reads = []

        def transport(tool, arguments):
            if tool == "fc_patch_clientes_by_id":
                raise TimeoutError("connection reset")
            reads.append(tool)
            if len(reads) == 1:
                return {"status": 200, "body": self.body}
            return {"status": 503, "body": {}}

        self.transport = transport
        outcome = self.enabled_writer().run_once()

        self.assertEqual(outcome.outcome, "retryable")
        self.assertEqual(outcome.reason, "outcome_unknown")

    def test_there_is_no_unconditional_write_path(self) -> None:
        """Spec 17: never substitute a plain PATCH when the predicate is absent."""
        source = (
            Path(__file__).resolve().parents[1] / "src/brain/famachat_client.py"
        ).read_text(encoding="utf-8")
        patch_calls = source.count("PATCH_CLIENT_TOOL,")

        self.assertEqual(patch_calls, 1)
        self.assertIn("CONDITIONAL_FIELD: expected_status", source)

    def test_the_frozen_strategy_matches_the_implementation(self) -> None:
        fixture = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "tests/fixtures/famachat-conditional-write-proof.json"
            ).read_text(encoding="utf-8")
        )
        from brain import famachat_client as fc

        self.assertEqual(fixture["verdict"], "PASS")
        self.assertEqual(fixture["field"], fc.CONDITIONAL_FIELD)
        self.assertEqual(fixture["refusal_http_status"], fc.CONFLICT_STATUS)
        self.assertEqual(fixture["tools"]["patch"], fc.PATCH_CLIENT_TOOL)
        self.assertEqual(fixture["tools"]["get"], fc.GET_CLIENT_TOOL)
        self.assertEqual(fixture["schema_fingerprint"], FINGERPRINT)

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
