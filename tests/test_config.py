from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def _write_production_config(self) -> Path:
        config_path = self.root / "brain.toml"
        config_path.write_text(
            f'''[server]
state_db = "{self.root / "state.db"}"
kanban_db = "{self.root / "kanban.db"}"

[principals.default]
mode = "gateway"
token_sha256 = "{token_digest("gateway")}"
tools = ["conversation_phone"]
''',
            encoding="utf-8",
        )
        return config_path

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

    def test_accepts_service_principals_and_new_capabilities(self) -> None:
        settings = BrainSettings(
            principals={
                "default": self._principal(
                    "default",
                    "gateway",
                    "gateway",
                    "conversation_context",
                    "turn_register",
                ),
                "observer": self._principal(
                    "observer", "service", "observer", "transport_ingest"
                ),
                "writer": self._principal(
                    "writer",
                    "service",
                    "writer",
                    "lifecycle_claim",
                    "lifecycle_result",
                ),
            },
            cursor_secret=b"c" * 32,
        )

        self.assertEqual(settings.principals["observer"].mode, "service")
        self.assertEqual(
            settings.principals["writer"].tools,
            frozenset({"lifecycle_claim", "lifecycle_result"}),
        )

    def test_runtime_settings_have_exact_defaults(self) -> None:
        settings = BrainSettings(
            principals={
                "default": self._principal(
                    "default", "gateway", "gateway", "conversation_phone"
                )
            },
            cursor_secret=b"c" * 32,
        )

        self.assertEqual(
            settings.runtime_db, Path("/var/lib/brain/runtime/brain-runtime.db")
        )
        self.assertEqual(
            settings.observer_session_dir,
            Path("/var/lib/brain/whatsapp-observer/session"),
        )
        self.assertEqual(settings.transport_retention_days, 90)
        self.assertEqual(settings.display_name_ttl_hours, 24)

    def test_rejects_non_positive_retention_settings(self) -> None:
        principals = {
            "default": self._principal(
                "default", "gateway", "gateway", "conversation_phone"
            )
        }
        with self.assertRaises(ValueError):
            BrainSettings(
                principals=principals,
                cursor_secret=b"c" * 32,
                transport_retention_days=0,
            )
        with self.assertRaises(ValueError):
            BrainSettings(
                principals=principals,
                cursor_secret=b"c" * 32,
                display_name_ttl_hours=0,
            )

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

        with patch.dict(
            os.environ,
            {
                "BRAIN_RUNTIME_HMAC_SECRET": "r" * 32,
                "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(set(settings.principals), {"default", "porteiro"})
        self.assertEqual(settings.whatsapp_session_dir, self.root / "session")

    def test_from_env_rejects_missing_runtime_hmac_secret(self) -> None:
        config_path = self._write_production_config()

        with (
            patch.dict(
                os.environ, {"BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32}, clear=True
            ),
            self.assertRaisesRegex(ValueError, "BRAIN_RUNTIME_HMAC_SECRET"),
        ):
            BrainSettings.from_env(config_path)

    def test_from_env_rejects_missing_transport_hmac_secret(self) -> None:
        config_path = self._write_production_config()

        with (
            patch.dict(os.environ, {"BRAIN_RUNTIME_HMAC_SECRET": "r" * 32}, clear=True),
            self.assertRaisesRegex(ValueError, "BRAIN_TRANSPORT_HMAC_SECRET"),
        ):
            BrainSettings.from_env(config_path)

    def test_from_env_rejects_short_runtime_hmac_secret(self) -> None:
        config_path = self._write_production_config()

        with (
            patch.dict(
                os.environ,
                {
                    "BRAIN_RUNTIME_HMAC_SECRET": "short",
                    "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "BRAIN_RUNTIME_HMAC_SECRET"),
        ):
            BrainSettings.from_env(config_path)

    def test_from_env_rejects_short_transport_hmac_secret(self) -> None:
        config_path = self._write_production_config()

        with (
            patch.dict(
                os.environ,
                {
                    "BRAIN_RUNTIME_HMAC_SECRET": "r" * 32,
                    "BRAIN_TRANSPORT_HMAC_SECRET": "short",
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "BRAIN_TRANSPORT_HMAC_SECRET"),
        ):
            BrainSettings.from_env(config_path)

    def test_from_env_accepts_distinct_raw_hmac_secrets(self) -> None:
        config_path = self._write_production_config()

        with patch.dict(
            os.environ,
            {
                "BRAIN_RUNTIME_HMAC_SECRET": "runtime-secret-domain-value-1234",
                "BRAIN_TRANSPORT_HMAC_SECRET": "transport-secret-domain-value-12",
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(
            settings.runtime_hmac_secret, b"runtime-secret-domain-value-1234"
        )
        self.assertEqual(
            settings.transport_hmac_secret, b"transport-secret-domain-value-12"
        )

    def test_from_env_decodes_64_hex_hmac_secrets(self) -> None:
        config_path = self._write_production_config()

        with patch.dict(
            os.environ,
            {
                "BRAIN_RUNTIME_HMAC_SECRET": "ab" * 32,
                "BRAIN_TRANSPORT_HMAC_SECRET": "cd" * 32,
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(settings.runtime_hmac_secret, bytes.fromhex("ab" * 32))
        self.assertEqual(settings.transport_hmac_secret, bytes.fromhex("cd" * 32))


if __name__ == "__main__":
    unittest.main()
