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
                ),
                "observer": self._principal(
                    "observer", "service", "observer", "transport_ingest"
                ),
            },
            cursor_secret=b"c" * 32,
        )

        self.assertEqual(settings.principals["observer"].mode, "service")
        self.assertNotIn("writer", settings.principals)

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
        self.assertEqual(settings.ctwa_raw_max_bytes, 4 * 1024 * 1024)
        self.assertEqual(settings.ctwa_raw_max_depth, 32)
        self.assertEqual(settings.ctwa_raw_max_nodes, 10_000)
        self.assertEqual(settings.context_response_max_bytes, 32 * 1024 * 1024)
        self.assertFalse(settings.meta_attribution_enabled)
        self.assertEqual(settings.meta_ad_account_id, "")
        self.assertEqual(settings.meta_ads_mcp_access_token, "")
        self.assertNotIn("fixture-token", repr(settings))
        self.assertEqual(settings.meta_ads_mcp_timeout_seconds, 4.0)
        self.assertEqual(settings.meta_ads_mcp_response_max_bytes, 8 * 1024 * 1024)
        self.assertEqual(settings.meta_ads_sync_interval_seconds, 900)
        self.assertEqual(settings.meta_ads_full_sync_interval_seconds, 86_400)

    def test_from_env_reads_enabled_meta_settings(self) -> None:
        config_path = self._write_production_config()
        with patch.dict(
            os.environ,
            {
                "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
                "BRAIN_META_ATTRIBUTION_ENABLED": "true",
                "BRAIN_META_AD_ACCOUNT_ID": "act_1598606388477916",
                "BRAIN_META_ADS_MCP_ACCESS_TOKEN": "fixture-token",
                "BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT": "2026-09-02T12:00:00Z",
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)
        self.assertTrue(settings.meta_attribution_enabled)
        self.assertEqual(settings.meta_ad_account_id, "1598606388477916")
        self.assertEqual(settings.meta_ads_mcp_access_token, "fixture-token")
        self.assertNotIn("fixture-token", repr(settings))
        self.assertEqual(settings.meta_ads_mcp_token_expires_at, 1788350400.0)

    def test_from_env_reads_raw_attribution_limits(self) -> None:
        config_path = self._write_production_config()
        with patch.dict(
            os.environ,
            {
                "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
                "BRAIN_CTWA_RAW_MAX_BYTES": "100",
                "BRAIN_CTWA_RAW_MAX_DEPTH": "3",
                "BRAIN_CTWA_RAW_MAX_NODES": "9",
                "BRAIN_CONTEXT_RESPONSE_MAX_BYTES": "101",
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(settings.ctwa_raw_max_bytes, 100)
        self.assertEqual(settings.ctwa_raw_max_depth, 3)
        self.assertEqual(settings.ctwa_raw_max_nodes, 9)
        self.assertEqual(settings.context_response_max_bytes, 101)

    def test_from_env_reads_raw_attribution_limits_from_toml(self) -> None:
        config_path = self.root / "brain.toml"
        config_path.write_text(
            f'''[server]
ctwa_raw_max_bytes = 100
ctwa_raw_max_depth = 3
ctwa_raw_max_nodes = 9
context_response_max_bytes = 101

[principals.default]
mode = "gateway"
token_sha256 = "{token_digest("gateway")}"
tools = ["conversation_phone"]
''',
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32},
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(
            (
                settings.ctwa_raw_max_bytes,
                settings.ctwa_raw_max_depth,
                settings.ctwa_raw_max_nodes,
                settings.context_response_max_bytes,
            ),
            (100, 3, 9, 101),
        )

    def test_rejects_invalid_raw_attribution_limits(self) -> None:
        principals = {
            "default": self._principal(
                "default", "gateway", "gateway", "conversation_phone"
            )
        }
        for field, value in (
            ("ctwa_raw_max_bytes", 0),
            ("ctwa_raw_max_depth", 0),
            ("ctwa_raw_max_nodes", 0),
            ("context_response_max_bytes", 0),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                BrainSettings(
                    principals=principals,
                    cursor_secret=b"c" * 32,
                    **{field: value},
                )
        with self.assertRaises(ValueError):
            BrainSettings(
                principals=principals,
                cursor_secret=b"c" * 32,
                ctwa_raw_max_bytes=101,
                context_response_max_bytes=100,
            )

    def test_from_env_rejects_non_integer_raw_attribution_limit_values(self) -> None:
        config_path = self._write_production_config()
        for value in ("true", "1.5", " 1", "+1"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
                        "BRAIN_CTWA_RAW_MAX_BYTES": value,
                    },
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "BRAIN_CTWA_RAW_MAX_BYTES"),
            ):
                BrainSettings.from_env(config_path)

    def test_from_env_rejects_boolean_and_float_toml_raw_limits(self) -> None:
        for value in ("true", "1.5"):
            with self.subTest(value=value):
                config_path = self.root / f"brain-{value}.toml"
                config_path.write_text(
                    f'''[server]
ctwa_raw_max_bytes = {value}

[principals.default]
mode = "gateway"
token_sha256 = "{token_digest("gateway")}"
tools = ["conversation_phone"]
''',
                    encoding="utf-8",
                )
                with (
                    patch.dict(
                        os.environ,
                        {"BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32},
                        clear=True,
                    ),
                    self.assertRaisesRegex(
                        (TypeError, ValueError), "ctwa_raw_max_bytes"
                    ),
                ):
                    BrainSettings.from_env(config_path)

    def test_observer_device_ids_default_empty_and_normalize(self) -> None:
        principals = {
            "default": self._principal(
                "default", "gateway", "gateway", "conversation_phone"
            )
        }
        self.assertEqual(
            BrainSettings(
                principals=principals, cursor_secret=b"c" * 32
            ).observer_device_ids,
            (),
        )

        settings = BrainSettings(
            principals=principals,
            cursor_secret=b"c" * 32,
            observer_device_ids=["obs-b", "obs-a", "obs-b"],
        )
        self.assertEqual(settings.observer_device_ids, ("obs-a", "obs-b"))

    def test_rejects_malformed_observer_device_id(self) -> None:
        principals = {
            "default": self._principal(
                "default", "gateway", "gateway", "conversation_phone"
            )
        }
        for invalid in ("", "has space", "x" * 129, "tab\there"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                BrainSettings(
                    principals=principals,
                    cursor_secret=b"c" * 32,
                    observer_device_ids=[invalid],
                )

    def test_from_env_reads_observer_device_ids(self) -> None:
        config_path = self._write_production_config()
        with patch.dict(
            os.environ,
            {
                "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
                "BRAIN_OBSERVER_DEVICE_IDS": " fama-observer-1 , fama-observer-2 ",
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(
            settings.observer_device_ids, ("fama-observer-1", "fama-observer-2")
        )

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
                "BRAIN_TRANSPORT_HMAC_SECRET": "t" * 32,
            },
            clear=True,
        ):
            settings = BrainSettings.from_env(config_path)

        self.assertEqual(set(settings.principals), {"default", "porteiro"})
        self.assertEqual(settings.whatsapp_session_dir, self.root / "session")

    def test_from_env_rejects_missing_transport_hmac_secret(self) -> None:
        config_path = self._write_production_config()

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "BRAIN_TRANSPORT_HMAC_SECRET"),
        ):
            BrainSettings.from_env(config_path)

    def test_from_env_rejects_short_transport_hmac_secret(self) -> None:
        config_path = self._write_production_config()

        with (
            patch.dict(
                os.environ,
                {
                    "BRAIN_TRANSPORT_HMAC_SECRET": "short",
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "BRAIN_TRANSPORT_HMAC_SECRET"),
        ):
            BrainSettings.from_env(config_path)

    def test_from_env_accepts_a_raw_hmac_secret(self) -> None:
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

        self.assertEqual(settings.transport_hmac_secret, bytes.fromhex("cd" * 32))


if __name__ == "__main__":
    unittest.main()
