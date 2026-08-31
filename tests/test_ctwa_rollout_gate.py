from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

spec = importlib.util.spec_from_file_location(
    "ctwa_rollout_gate", SCRIPTS / "ctwa_rollout_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

FINGERPRINT = "4da4e773feb862db22b273d159fc9ba456549df0052e52d2d90d00a17f34fd11"


class RolloutGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.results = self.root / "results.json"
        self.proof = self.root / "proof.json"
        self.schema = self.root / "schema.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_results(self, **overrides) -> None:
        gates = {name: "PASS" for name in gate.SHADOW_GATES}
        gates.update({name: "PASS" for name in gate.WRITE_GATES})
        gates.update(overrides)
        self.results.write_text(json.dumps({"gates": gates}), encoding="utf-8")

    def write_proof(self, **overrides) -> None:
        proof = {"verdict": "PASS", "schema_fingerprint": FINGERPRINT}
        proof.update(overrides)
        self.proof.write_text(json.dumps(proof), encoding="utf-8")

    def write_schema(self, fingerprint: str = FINGERPRINT) -> None:
        self.schema.write_text(
            json.dumps({"fingerprint": fingerprint}), encoding="utf-8"
        )

    def evaluate(self, mode: str):
        return gate.evaluate(
            mode,
            results_path=self.results,
            proof_path=self.proof,
            schema_path=self.schema,
        )

    # ------------------------------------------------------------------

    def test_complete_evidence_permits_shadow(self) -> None:
        self.write_results()

        allowed, _ = self.evaluate("shadow")

        self.assertTrue(allowed)

    def test_missing_evidence_is_a_failure_not_a_pass(self) -> None:
        """A gate that treats absence as safety reports what it never checked."""
        allowed, verdicts = self.evaluate("shadow")

        self.assertFalse(allowed)
        self.assertTrue(all(status == "MISSING" for _, status in verdicts))

    def test_any_single_failing_gate_blocks_shadow(self) -> None:
        for name in gate.SHADOW_GATES:
            with self.subTest(gate=name):
                self.write_results(**{name: "FAIL"})
                allowed, _ = self.evaluate("shadow")
                self.assertFalse(allowed)

    def test_not_proven_is_not_a_pass(self) -> None:
        self.write_results(LIFECYCLE_SHADOW="NOT_PROVEN")

        allowed, _ = self.evaluate("shadow")

        self.assertFalse(allowed)

    # ------------------------------------------------------------------

    def test_write_needs_everything_shadow_needs_and_more(self) -> None:
        self.write_results()
        self.write_proof()
        self.write_schema()

        allowed, verdicts = self.evaluate("write")

        self.assertTrue(allowed)
        names = [name for name, _ in verdicts]
        for name in gate.SHADOW_GATES + gate.WRITE_GATES:
            self.assertIn(name, names)

    def test_shadow_passing_does_not_permit_write(self) -> None:
        self.write_results()

        self.assertTrue(self.evaluate("shadow")[0])
        self.assertFalse(self.evaluate("write")[0])

    def test_write_needs_a_passing_conditional_proof(self) -> None:
        self.write_results()
        self.write_schema()

        for proof in ({"verdict": "FAIL"}, None):
            with self.subTest(proof=proof):
                if proof is None:
                    self.proof.unlink(missing_ok=True)
                else:
                    self.proof.write_text(json.dumps(proof), encoding="utf-8")
                allowed, verdicts = self.evaluate("write")
                self.assertFalse(allowed)
                self.assertIn(("FAMACHAT_CONDITIONAL_WRITE", "NOT_PROVEN"), verdicts)

    def test_a_changed_live_schema_invalidates_the_proof(self) -> None:
        """The proof describes the server it was taken against, not today's."""
        self.write_results()
        self.write_proof()
        self.write_schema(fingerprint="0" * 64)

        allowed, verdicts = self.evaluate("write")

        self.assertFalse(allowed)
        self.assertIn(("CONDITIONAL_SCHEMA_FINGERPRINT_MATCH", "MISMATCH"), verdicts)

    def test_a_missing_schema_capture_blocks_write(self) -> None:
        self.write_results()
        self.write_proof()

        allowed, verdicts = self.evaluate("write")

        self.assertFalse(allowed)
        self.assertIn(("CONDITIONAL_SCHEMA_FINGERPRINT_MATCH", "MISMATCH"), verdicts)

    def test_write_needs_the_dry_run_and_crash_recovery_evidence(self) -> None:
        self.write_proof()
        self.write_schema()
        for name in ("WRITER_DRY_RUN", "CRASH_AFTER_WRITE_RECOVERY"):
            with self.subTest(gate=name):
                self.write_results(**{name: "MISSING"})
                self.assertFalse(self.evaluate("write")[0])

    def test_malformed_evidence_is_treated_as_absent(self) -> None:
        self.results.write_text("not json", encoding="utf-8")

        allowed, verdicts = self.evaluate("shadow")

        self.assertFalse(allowed)
        self.assertTrue(all(status == "MISSING" for _, status in verdicts))

    # ------------------------------------------------------------------

    def test_the_gate_only_observes(self) -> None:
        """It reports a verdict; enabling writes stays a human action."""
        source = (SCRIPTS / "ctwa_rollout_gate.py").read_text(encoding="utf-8")

        for forbidden in (
            "systemctl",
            "write_text",
            "subprocess",
            "os.environ[",
            "BRAIN_LIFECYCLE_WRITE_ENABLED",
        ):
            self.assertNotIn(forbidden, source)

    def test_the_verdict_carries_no_lead_information(self) -> None:
        self.write_results()
        self.write_proof()
        self.write_schema()

        _, verdicts = self.evaluate("write")

        for name, status in verdicts:
            self.assertRegex(name, r"^[A-Z0-9_]+$")
            self.assertRegex(status, r"^[A-Z_]+$")


if __name__ == "__main__":
    unittest.main()
