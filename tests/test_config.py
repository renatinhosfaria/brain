from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from brain.config import BrainSettings, PrincipalConfig, token_digest


class BrainSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _principal(
        self, name: str, mode: str, token: str, *tools: str
    ) -> PrincipalConfig:
        return PrincipalConfig(name, mode, token_digest(token), frozenset(tools))

    def test_accepts_dynamic_worker_and_gateway_principals(self) -> None:
        settings = BrainSettings(
            state_db=self.root / "state.db",
            kanban_db=self.root / "kanban.db",
            whatsapp_session_dir=self.root / "session",
            principals={
                "default": self._principal(
                    "default", "gateway", "gateway", "conversation_phone"
                ),
                "porteiro": self._principal(
                    "porteiro", "worker", "porteiro", "conversation_phone"
                ),
                "reno": self._principal(
                    "reno",
                    "worker",
                    "reno",
                    "conversation_recent",
                    "conversation_search",
                ),
            },
            cursor_secret=b"c" * 32,
        )

        self.assertEqual(settings.principals["default"].mode, "gateway")
        self.assertEqual(settings.whatsapp_session_dir, self.root / "session")

    def test_rejects_invalid_principal_mode(self) -> None:
        with self.assertRaises(ValueError):
            BrainSettings(
                state_db=self.root / "state.db",
                kanban_db=self.root / "kanban.db",
                principals={
                    "default": self._principal(
                        "default", "side-channel", "gateway", "conversation_phone"
                    ),
                },
                cursor_secret=b"c" * 32,
            )

    def test_rejects_unknown_principal_tool(self) -> None:
        with self.assertRaises(ValueError):
            BrainSettings(
                state_db=self.root / "state.db",
                kanban_db=self.root / "kanban.db",
                principals={
                    "default": self._principal(
                        "default",
                        "gateway",
                        "gateway",
                        "conversation_phone",
                        "read_all",
                    ),
                },
                cursor_secret=b"c" * 32,
            )

    def test_rejects_duplicate_principal_digest(self) -> None:
        digest = token_digest("same-secret")
        with self.assertRaises(ValueError):
            BrainSettings(
                state_db=self.root / "state.db",
                kanban_db=self.root / "kanban.db",
                principals={
                    "default": PrincipalConfig(
                        "default", "gateway", digest, frozenset({"conversation_phone"})
                    ),
                    "porteiro": PrincipalConfig(
                        "porteiro", "worker", digest, frozenset({"conversation_phone"})
                    ),
                },
                cursor_secret=b"c" * 32,
            )

    def test_from_env_loads_principals_and_mapping_directory(self) -> None:
        config_path = self.root / "brain.toml"
        state_db = self.root / "state.db"
        kanban_db = self.root / "kanban.db"
        mapping_dir = self.root / "session"
        gateway_digest = token_digest("gateway")
        porteiro_digest = token_digest("porteiro")
        config_path.write_text(
            f'''[server]
state_db = "{state_db}"
kanban_db = "{kanban_db}"
whatsapp_session_dir = "{mapping_dir}"
cursor_secret = "{"c" * 64}"

[principals.default]
mode = "gateway"
token_sha256 = "{gateway_digest}"
tools = ["conversation_phone"]

[principals.porteiro]
mode = "worker"
token_sha256 = "{porteiro_digest}"
tools = ["conversation_phone"]
''',
            encoding="utf-8",
        )

        settings = BrainSettings.from_env(config_path)

        self.assertEqual(set(settings.principals), {"default", "porteiro"})
        self.assertEqual(settings.whatsapp_session_dir, self.root / "session")


if __name__ == "__main__":
    unittest.main()
