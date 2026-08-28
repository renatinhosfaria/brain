from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
