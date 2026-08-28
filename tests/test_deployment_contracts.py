from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.install_brain_secrets import restore_files, snapshot_files

ROOT = Path(__file__).parents[1]


class DeploymentContractTests(unittest.TestCase):
    def test_brain_toml_example_declares_v2_principals(self) -> None:
        data = tomllib.loads(
            (ROOT / "deploy/brain.toml.example").read_text(encoding="utf-8")
        )

        self.assertEqual(data["principals"]["default"]["mode"], "gateway")
        self.assertEqual(
            data["principals"]["porteiro"]["tools"], ["conversation_phone"]
        )
        self.assertIn("whatsapp_session_dir", data["server"])

    def test_smoke_expected_tools_are_v2_surface(self) -> None:
        source = (ROOT / "scripts/smoke_test.py").read_text(encoding="utf-8")

        self.assertIn('"conversation_phone"', source)
        self.assertIn('"whatsapp_identity": "compatible"', source)
        self.assertIn('"gateway_bridge": "configured"', source)

    def test_docs_define_core_boundary_and_phone_invariant(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        invariant = (ROOT / "docs/conversation-identity-invariant.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("/usr/local/lib/hermes-agent", readme)
        self.assertIn("runtime determina a conversa", invariant)

    def test_ceo_example_scopes_bridge_to_whatsapp(self) -> None:
        source = (ROOT / "deploy/hermes-ceo-brain.example.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("brain-ceo-bridge", source)
        self.assertIn("brain-context", source)
        self.assertIn("whatsapp:", source)
        self.assertIn("Do not add `brain-context` to cli/telegram", source)

    def test_hermes_check_covers_gateway_plugin_and_context_scope(self) -> None:
        source = (ROOT / "scripts/hermes_integration_check.py").read_text(
            encoding="utf-8"
        )

        for required in (
            "BRAIN_GATEWAY_TOKEN",
            "doctor_plugin",
            "brain-context",
            "HERMES_SESSION_PLATFORM",
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_KEY",
            "HERMES_SESSION_ID",
        ):
            self.assertIn(required, source)

    def test_hermes_check_validates_principal_modes_and_tool_acl(self) -> None:
        source = (ROOT / "scripts/hermes_integration_check.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("expected_modes", source)
        self.assertIn('principal.get("mode")', source)
        self.assertIn('principal.get("tools")', source)

    def test_smoke_covers_authenticated_gateway_probe(self) -> None:
        source = (ROOT / "scripts/smoke_test.py").read_text(encoding="utf-8")

        self.assertIn("BRAIN_GATEWAY_TOKEN", source)
        self.assertIn("gateway/conversation-phone", source)
        self.assertIn("Content-Length", source)

    def test_secret_installation_restores_all_snapshots_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "brain.toml"
            created = root / "brain.env"
            existing.write_bytes(b"before")

            snapshots = snapshot_files((existing, created))
            existing.write_bytes(b"partial update")
            created.write_bytes(b"partial secret")
            restore_files(snapshots)

            self.assertEqual(existing.read_bytes(), b"before")
            self.assertFalse(created.exists())

    def test_docs_cover_ceo_scope_and_complete_rollback(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")

        self.assertIn("hermes-ceo-brain.example.yaml", readme)
        self.assertIn("WhatsApp only", readme)
        for required in (
            "brain-context",
            "disable `brain-ceo-bridge`",
            "Porteiro",
            "Cadastro",
        ):
            self.assertIn(required, runbook)

    def test_v2_metadata_describes_identity_and_implementation_status(self) -> None:
        prd = (ROOT / "PRD.md").read_text(encoding="utf-8")
        package = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        service = (ROOT / "deploy/brain.service").read_text(encoding="utf-8")

        self.assertIn("Status:** Implementada", prd)
        self.assertIn("identity", package.lower())
        self.assertIn("identity", service.lower())

    def test_worker_templates_preserve_famachat_with_brain(self) -> None:
        for filename in (
            "deploy/hermes-brain.example.yaml",
            "deploy/hermes-brain-memory.example.yaml",
        ):
            source = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("famachat", source, filename)
            self.assertIn("- brain", source, filename)

        famaagent = (ROOT / "deploy/hermes-brain-famaagent.example.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("- brain", famaagent)
        self.assertNotIn("famachat", famaagent)

    def test_hermes_check_requires_famachat_by_profile(self) -> None:
        source = (ROOT / "scripts/hermes_integration_check.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"famachat"', source)
        self.assertIn("expected_cli_toolsets", source)
        for profile in ("porteiro", "cadastro", "reno", "famaagent"):
            self.assertIn(f'"{profile}"', source)

    def test_ci_formats_and_lints_the_external_plugin(self) -> None:
        source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("uv run ruff check src tests scripts integrations", source)
        self.assertIn(
            "uv run ruff format --check src tests scripts integrations", source
        )


if __name__ == "__main__":
    unittest.main()
