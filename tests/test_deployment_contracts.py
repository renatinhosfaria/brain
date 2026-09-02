from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain.config import BrainSettings, PrincipalConfig, token_digest
from scripts.install_brain_secrets import restore_files, snapshot_files

ROOT = Path(__file__).parents[1]


class DeploymentContractTests(unittest.TestCase):
    def test_meta_credential_installer_rotates_atomically_without_echoing_token(
        self,
    ) -> None:
        """Removing the atomic installer must break secret rotation, not logging."""
        from scripts import install_meta_ads_credential

        token = "synthetic-meta-token-never-log"
        expiry = "2026-11-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            env_path = config_dir / "brain.env"
            env_path.write_text(
                "BRAIN_CONFIG=/etc/brain/brain.toml\nOTHER_VALUE=preserved\n"
                "BRAIN_META_ADS_MCP_ACCESS_TOKEN=old-value\n",
                encoding="utf-8",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch.object(
                    install_meta_ads_credential.sys, "stdin", io.StringIO(token + "\n")
                ),
            ):
                result = install_meta_ads_credential.main(
                    [
                        "--config-dir",
                        str(config_dir),
                        "--expires-at",
                        expiry,
                        "--rotate",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                stdout.getvalue(), "OK: Meta Ads MCP credential installed\n"
            )
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "BRAIN_CONFIG=/etc/brain/brain.toml\nOTHER_VALUE=preserved\n"
                f"BRAIN_META_ADS_MCP_ACCESS_TOKEN={token}\n"
                f"BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT={expiry}\n",
            )

    def test_meta_credential_installer_runs_from_its_documented_script_path(
        self,
    ) -> None:
        """A broken direct-script import must fail before an operator supplies a token."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/install_meta_ads_credential.py", "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config-dir", result.stdout)

    def test_meta_credential_installer_refuses_replacement_and_symlinks(self) -> None:
        """Removing overwrite or symlink guards must fail this installation boundary."""
        from scripts import install_meta_ads_credential

        token = "synthetic-meta-token-never-log"
        expiry = "2026-11-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            env_path = config_dir / "brain.env"
            env_path.write_text(
                "OTHER_VALUE=preserved\nBRAIN_META_ADS_MCP_ACCESS_TOKEN=old-value\n",
                encoding="utf-8",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch.object(
                    install_meta_ads_credential.sys, "stdin", io.StringIO(token + "\n")
                ),
            ):
                result = install_meta_ads_credential.main(
                    ["--config-dir", str(config_dir), "--expires-at", expiry]
                )
            self.assertEqual(result, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(), "FAIL: Meta Ads MCP credential installation failed\n"
            )
            self.assertNotIn(token, stderr.getvalue())
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "OTHER_VALUE=preserved\nBRAIN_META_ADS_MCP_ACCESS_TOKEN=old-value\n",
            )

            target = config_dir / "outside.env"
            target.write_text("OUTSIDE=unchanged\n", encoding="utf-8")
            env_path.unlink()
            env_path.symlink_to(target)
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch.object(
                    install_meta_ads_credential.sys, "stdin", io.StringIO(token + "\n")
                ),
            ):
                result = install_meta_ads_credential.main(
                    [
                        "--config-dir",
                        str(config_dir),
                        "--expires-at",
                        expiry,
                        "--rotate",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual(
                stderr.getvalue(), "FAIL: Meta Ads MCP credential installation failed\n"
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "OUTSIDE=unchanged\n")

            env_path.unlink()
            env_path.write_text("OTHER_VALUE=preserved\n", encoding="utf-8")
            linked_dir = config_dir / "linked-brain-config"
            linked_dir.symlink_to(config_dir, target_is_directory=True)
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch.object(
                    install_meta_ads_credential.sys, "stdin", io.StringIO(token + "\n")
                ),
            ):
                result = install_meta_ads_credential.main(
                    [
                        "--config-dir",
                        str(linked_dir),
                        "--expires-at",
                        expiry,
                        "--rotate",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual(
                stderr.getvalue(), "FAIL: Meta Ads MCP credential installation failed\n"
            )
            self.assertEqual(
                env_path.read_text(encoding="utf-8"), "OTHER_VALUE=preserved\n"
            )

    def test_meta_credential_installer_restores_snapshot_after_write_failure(
        self,
    ) -> None:
        """Removing rollback after an interrupted write must leave a partial secret."""
        from scripts import install_meta_ads_credential

        token = "synthetic-meta-token-never-log"
        expiry = "2026-11-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            env_path = config_dir / "brain.env"
            original = b"OTHER_VALUE=preserved\n"
            env_path.write_bytes(original)
            real_write = install_meta_ads_credential.atomic_private_write_bytes

            def fail_after_partial(path: Path, content: bytes) -> None:
                real_write(path, b"PARTIAL=write\n")
                raise OSError("injected failure")

            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch.object(
                    install_meta_ads_credential.sys, "stdin", io.StringIO(token + "\n")
                ),
                patch.object(
                    install_meta_ads_credential,
                    "atomic_private_write_bytes",
                    side_effect=fail_after_partial,
                ),
            ):
                result = install_meta_ads_credential.main(
                    [
                        "--config-dir",
                        str(config_dir),
                        "--expires-at",
                        expiry,
                        "--rotate",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual(env_path.read_bytes(), original)
            self.assertEqual(
                stderr.getvalue(), "FAIL: Meta Ads MCP credential installation failed\n"
            )
            self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())

    def test_meta_credential_installer_rejects_expiries_the_runtime_rejects(
        self,
    ) -> None:
        """Permissive ISO parsing must not write a credential Brain cannot load."""
        from scripts import install_meta_ads_credential

        token = "synthetic-meta-token-never-log"
        original = b"OTHER_VALUE=preserved\n"
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            env_path = config_dir / "brain.env"
            for expiry in ("2026-W36-2T12:00:00Z", "2026-09-02T12:00:00,123Z"):
                with self.subTest(expiry=expiry):
                    env_path.write_bytes(original)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                        patch.object(
                            install_meta_ads_credential.sys,
                            "stdin",
                            io.StringIO(token + "\n"),
                        ),
                    ):
                        result = install_meta_ads_credential.main(
                            [
                                "--config-dir",
                                str(config_dir),
                                "--expires-at",
                                expiry,
                                "--rotate",
                            ]
                        )
                    self.assertEqual(result, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(
                        stderr.getvalue(),
                        "FAIL: Meta Ads MCP credential installation failed\n",
                    )
                    self.assertEqual(env_path.read_bytes(), original)
                    self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())

    def test_meta_probe_validates_credential_while_feature_stays_disabled(
        self,
    ) -> None:
        """Enabling the resolver just to probe a credential breaks the rollout order."""
        from scripts import meta_ads_mcp_probe

        class FakeClient:
            def __init__(self, configured: object) -> None:
                self.configured = configured
                self.probed = False
                self.ad_id: str | None = None

            def probe(self) -> object:
                self.probed = True
                return object()

            def get_ad(self, source_id: str, now: float) -> object:
                self.ad_id = source_id
                if not self.probed or now <= 0:
                    raise AssertionError("probe must precede known-ad read")
                return object()

        created: list[FakeClient] = []

        def factory(configured: object) -> FakeClient:
            client = FakeClient(configured)
            created.append(client)
            return client

        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "brain.toml"
            config_path.write_text(
                """[server]
meta_attribution_enabled = false
meta_ad_account_id = "act_1598606388477916"
cursor_secret = "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"

[principals.default]
mode = "gateway"
token_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
tools = ["conversation_context"]
""",
                encoding="utf-8",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch.dict(
                    os.environ,
                    {
                        "BRAIN_CONFIG": str(config_path),
                        "BRAIN_TRANSPORT_HMAC_SECRET": "x" * 32,
                        "BRAIN_META_ADS_MCP_ACCESS_TOKEN": "synthetic-meta-token-never-log",
                        "BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT": "2026-11-01T00:00:00Z",
                        "BRAIN_META_PROBE_AD_ID": "1203001",
                    },
                    clear=True,
                ),
            ):
                result = meta_ads_mcp_probe.main(client_factory=factory)
        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue(),
            "OK: Meta Ads MCP tools, configured account, and known-ad read verified\n",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].configured.meta_attribution_enabled)
        self.assertEqual(created[0].configured.meta_ad_account_id, "1598606388477916")
        self.assertEqual(created[0].ad_id, "1203001")

    def test_meta_probe_hides_token_known_id_and_exception_details(self) -> None:
        """Replacing bounded errors with exception echoing must expose this regression."""
        from scripts import meta_ads_mcp_probe

        token = "synthetic-meta-token-never-log"
        known_id = "1203001"
        settings = SimpleNamespace(
            meta_attribution_enabled=True,
            meta_ad_account_id="1598606388477916",
            meta_ads_mcp_access_token=token,
            meta_ads_mcp_token_expires_at=1_793_491_200.0,
        )

        class FailingClient:
            def __init__(self, configured: object) -> None:
                pass

            def probe(self) -> object:
                raise RuntimeError(f"{token} {known_id} confidential response")

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch.object(
                meta_ads_mcp_probe.BrainSettings, "from_env", return_value=settings
            ),
            patch.dict(os.environ, {"BRAIN_META_PROBE_AD_ID": known_id}, clear=False),
        ):
            result = meta_ads_mcp_probe.main(client_factory=FailingClient)
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "FAIL: meta_server_unavailable\n")
        self.assertNotIn(token, stderr.getvalue())
        self.assertNotIn(known_id, stderr.getvalue())

    def test_oauth_probe_uses_the_fixed_account_while_worker_is_disabled(self) -> None:
        """OAuth validation must precede enabling the attribution worker."""
        from scripts import meta_ads_oauth

        settings = BrainSettings(
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-token"),
                    frozenset({"conversation_context"}),
                )
            },
            cursor_secret=b"c" * 32,
        )
        created: list[object] = []

        class FakeClient:
            def __init__(
                self, configured: object, *, credential_provider: object
            ) -> None:
                self.configured = configured
                self.credential_provider = credential_provider
                created.append(self)

            def probe(self) -> object:
                return object()

        args = SimpleNamespace(
            store_path=Path("/unused/store"), key_path=Path("/unused/key")
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch.object(
                meta_ads_oauth.BrainSettings, "from_env", return_value=settings
            ),
            patch.object(meta_ads_oauth, "_oauth", return_value=object()),
            patch.object(
                meta_ads_oauth, "OAuthCredentialProvider", return_value="provider"
            ),
            patch.object(meta_ads_oauth, "MetaAdsMcpClient", FakeClient),
        ):
            result = meta_ads_oauth._probe(args)

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            stdout.getvalue(), "OK: Meta Ads OAuth read-only MCP probe verified\n"
        )
        self.assertEqual(len(created), 1)
        configured = created[0].configured
        self.assertFalse(configured.meta_attribution_enabled)
        self.assertEqual(configured.meta_ad_account_id, "1598606388477916")
        self.assertEqual(configured.meta_ads_mcp_auth_mode, "oauth")

    def test_oauth_status_reports_missing_store_before_configuration(self) -> None:
        from scripts import meta_ads_oauth

        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(
                store_path=Path(temporary) / "missing.enc",
                key_path=Path(temporary) / "missing.key",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = meta_ads_oauth._status(args)

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "Meta Ads OAuth status: missing\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_oauth_cli_has_no_pre_registered_configure_command(self) -> None:
        from scripts import meta_ads_oauth

        with self.assertRaises(SystemExit):
            meta_ads_oauth._parser().parse_args(["configure"])

    def test_oauth_login_uses_dynamic_store_without_secret_prompts(self) -> None:
        from scripts import meta_ads_oauth

        request = SimpleNamespace(url="https://example.invalid/oauth", state="state")
        calls: list[str] = []

        class FakeOAuth:
            def authorization_url(self) -> object:
                calls.append("authorization_url")
                return request

            def exchange_code(self, code: str, received: object) -> object:
                calls.append(f"exchange:{code}")
                self.received = received
                return object()

            def save_credentials(self, credentials: object) -> None:
                calls.append("save")

        args = SimpleNamespace(
            store_path=Path("/unused/store"), key_path=Path("/unused/key")
        )
        with (
            patch.object(meta_ads_oauth, "_oauth", return_value=FakeOAuth()),
            patch.object(
                meta_ads_oauth.OAuthCallback,
                "serve_once",
                return_value="authorization-code",
            ),
            patch("builtins.print") as output,
        ):
            result = meta_ads_oauth._login(args)

        self.assertEqual(result, 0)
        self.assertEqual(
            calls, ["authorization_url", "exchange:authorization-code", "save"]
        )
        self.assertIn(
            "https://example.invalid/oauth",
            " ".join(str(c) for c in output.call_args_list),
        )

    def test_service_oauth_constructs_dcr_provider_without_app_configuration(
        self,
    ) -> None:
        from brain import service as brain_service

        settings = BrainSettings(
            meta_attribution_enabled=True,
            meta_ad_account_id="act_1598606388477916",
            meta_ads_mcp_auth_mode="oauth",
            principals={
                "default": PrincipalConfig(
                    "default",
                    "gateway",
                    token_digest("gateway-token"),
                    frozenset({"conversation_context"}),
                )
            },
            cursor_secret=b"c" * 32,
            transport_hmac_secret=b"t" * 32,
        )
        dynamic_oauth = object()
        with (
            patch.object(
                brain_service.MetaAdsOAuth,
                "from_store_or_new",
                return_value=dynamic_oauth,
            ) as from_store_or_new,
            patch.object(
                brain_service.MetaAdsOAuth,
                "from_store",
                side_effect=AssertionError("legacy OAuth constructor used"),
            ),
            patch.object(
                brain_service, "OAuthCredentialProvider", return_value="provider"
            ) as provider_factory,
            patch.object(brain_service, "MetaAttributionService"),
            patch.object(brain_service.RuntimeDatabase, "initialize"),
        ):
            brain_service.BrainService(settings)

        from_store_or_new.assert_called_once()
        provider_factory.assert_called_once_with(dynamic_oauth)

    def test_meta_deployment_examples_smoke_and_runbook_preserve_read_only_contract(
        self,
    ) -> None:
        """Wrong account/defaults or secret-like examples must fail the deployment gate."""
        env_example = (ROOT / "deploy/brain.env.example").read_text(encoding="utf-8")
        toml = tomllib.loads(
            (ROOT / "deploy/brain.toml.example").read_text(encoding="utf-8")
        )
        service = (ROOT / "deploy/brain.service").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts/smoke_test.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")

        self.assertIn(
            "BRAIN_META_ADS_MCP_ACCESS_TOKEN=replace-with-meta-ads-mcp-access-token",
            env_example,
        )
        self.assertIn(
            "BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT=2099-01-01T00:00:00Z",
            env_example,
        )
        self.assertNotRegex(env_example, r"(?i)EA[A-Za-z0-9_-]{20,}")
        server = toml["server"]
        self.assertFalse(server["meta_attribution_enabled"])
        self.assertEqual(server["meta_ad_account_id"], "act_1598606388477916")
        self.assertEqual(server["meta_ads_mcp_timeout_seconds"], 4.0)
        self.assertEqual(server["meta_ads_mcp_response_max_bytes"], 8_388_608)
        self.assertEqual(server["meta_ads_sync_interval_seconds"], 900)
        self.assertEqual(server["meta_ads_full_sync_interval_seconds"], 86_400)
        for required in (
            "User=root",
            "Group=root",
            "AF_INET",
            "AF_INET6",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/brain/runtime",
        ):
            self.assertIn(required, service)
        self.assertIn('meta_status == "disabled"', smoke)
        self.assertIn('{"ready", "degraded"}', smoke)
        for source in (readme, runbook):
            self.assertIn("act_1598606388477916", source)
            self.assertIn("https://mcp.facebook.com/ads", source)
        for source in (readme, runbook):
            self.assertIn("meta_ads_oauth.py clear", source)
            self.assertIn("meta_ads_oauth.py login", source)
            self.assertNotIn("meta_ads_oauth.py configure", source)
        for required in (
            "Create & manage ads with Ads MCP server",
            "Read/Manage",
            "--rotate",
            "rollback",
            "pending",
            "disabled",
            "ready",
            "degraded",
            "90-day",
            "ads_management",
            "https://www.postman.com/meta/whatsapp-business-platform/request/g7sv9jo/received-message-triggered-by-click-to-whatsapp-ads",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runbook)

    def test_whatsapp_observer_unit_is_hardened_and_isolated(self) -> None:
        source = (ROOT / "deploy/brain-whatsapp-observer.service").read_text(
            encoding="utf-8"
        )

        for required in (
            "[Service]",
            "Type=simple",
            "UMask=0077",
            "Restart=on-failure",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/brain/whatsapp-observer",
            "EnvironmentFile=/etc/brain/whatsapp-observer.env",
            "observers/whatsapp/src/main.mjs",
        ):
            self.assertIn(required, source)
        exec_start_lines = [
            line for line in source.splitlines() if line.startswith("ExecStart=")
        ]
        self.assertEqual(
            exec_start_lines,
            [
                (
                    "ExecStart=/opt/brain/node/bin/node "
                    "/root/brain/observers/whatsapp/src/main.mjs"
                )
            ],
        )
        for forbidden in (
            "/usr/bin/node",
            "/usr/local/bin/node",
            "/root/.hermes/node",
            "/usr/local/lib/hermes-agent",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("/root/.hermes/platforms/whatsapp/session", source)
        self.assertNotIn("send", source.lower())
        read_write_lines = [
            line for line in source.splitlines() if line.startswith("ReadWritePaths=")
        ]
        self.assertEqual(
            read_write_lines,
            ["ReadWritePaths=/var/lib/brain/whatsapp-observer"],
        )

    def test_whatsapp_observer_env_uses_only_its_own_paths_and_secrets(self) -> None:
        source = (ROOT / "deploy/brain-whatsapp-observer.env.example").read_text(
            encoding="utf-8"
        )

        for required in (
            "BRAIN_OBSERVER_SESSION_DIR=/var/lib/brain/whatsapp-observer/session",
            "BRAIN_OBSERVER_OUTBOX_DIR=/var/lib/brain/whatsapp-observer/outbox",
            "BRAIN_OBSERVER_TOKEN=<observer-service-token>",
            "BRAIN_OBSERVER_DEVICE_ID=<observer-device-id>",
            "BRAIN_TRANSPORT_HMAC_SECRET=<at-least-32-byte-transport-secret>",
            "BRAIN_URL=http://127.0.0.1:8765",
            "BRAIN_OBSERVER_HEALTH_HOST=127.0.0.1",
            "BRAIN_OBSERVER_HEALTH_PORT=8775",
        ):
            self.assertIn(required, source)
        self.assertNotIn("BRAIN_RUNTIME_HMAC_SECRET", source)
        self.assertNotIn("/root/.hermes/platforms/whatsapp/session", source)
        self.assertNotIn("/usr/local/lib/hermes-agent", source)
        self.assertNotRegex(source, r"(?i)\b[0-9a-f]{64}\b")

    def test_whatsapp_observer_baileys_dependencies_remain_exactly_pinned(self) -> None:
        package = json.loads(
            (ROOT / "observers/whatsapp/package.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            package["dependencies"]["@whiskeysockets/baileys"], "7.0.0-rc13"
        )
        self.assertEqual(package["dependencies"]["qrcode-terminal"], "0.12.0")

    def test_brain_toml_example_declares_v2_principals(self) -> None:
        data = tomllib.loads(
            (ROOT / "deploy/brain.toml.example").read_text(encoding="utf-8")
        )

        self.assertEqual(data["principals"]["default"]["mode"], "gateway")
        self.assertEqual(
            data["principals"]["porteiro"]["tools"], ["conversation_phone"]
        )
        self.assertIn("whatsapp_session_dir", data["server"])

    def test_runtime_and_observer_paths_are_private_and_separate(self) -> None:
        data = tomllib.loads(
            (ROOT / "deploy/brain.toml.example").read_text(encoding="utf-8")
        )

        self.assertEqual(
            data["server"]["runtime_db"],
            "/var/lib/brain/runtime/brain-runtime.db",
        )
        self.assertEqual(
            data["server"]["observer_session_dir"],
            "/var/lib/brain/whatsapp-observer/session",
        )
        self.assertNotEqual(
            data["server"]["observer_session_dir"],
            data["server"]["whatsapp_session_dir"],
        )

    def test_env_example_has_distinct_hmac_domains_and_principal_credentials(
        self,
    ) -> None:
        source = (ROOT / "deploy/brain.env.example").read_text(encoding="utf-8")

        for name in (
            "BRAIN_TRANSPORT_HMAC_SECRET",
            "BRAIN_GATEWAY_TOKEN",
            "BRAIN_OBSERVER_TOKEN",
        ):
            self.assertIn(f"{name}=", source)
        self.assertIn("distinct", source.lower())
        self.assertNotRegex(source, r"(?i)\b[0-9a-f]{64}\b")

    def test_gateway_and_service_principal_capabilities_are_exact(self) -> None:
        data = tomllib.loads(
            (ROOT / "deploy/brain.toml.example").read_text(encoding="utf-8")
        )

        self.assertEqual(
            data["principals"]["default"]["tools"],
            ["conversation_context"],
        )
        self.assertEqual(data["principals"]["observer"]["tools"], ["transport_ingest"])
        # Amendment 2: Brain holds no FamaChat credential and no writer
        # principal. Reno owns the transitions through its own MCP surface.
        self.assertNotIn("writer", data["principals"])

    def test_service_example_is_localhost_private_and_documents_runtime_permissions(
        self,
    ) -> None:
        source = (ROOT / "deploy/brain.service").read_text(encoding="utf-8")

        self.assertIn("127.0.0.1", source)
        self.assertIn("/var/lib/brain/runtime", source)
        self.assertIn("0700", source)
        self.assertIn("0600", source)
        self.assertNotIn("0.0.0.0", source)

    def test_examples_do_not_enable_observer_or_lifecycle_writes(self) -> None:
        unit = (ROOT / "deploy/brain.service").read_text(encoding="utf-8")
        ceo = (ROOT / "deploy/hermes-ceo-brain.example.yaml").read_text(
            encoding="utf-8"
        )
        active_ceo = "\n".join(
            line for line in ceo.splitlines() if not line.lstrip().startswith("#")
        )

        self.assertNotIn("whatsapp-observer.service", unit)
        self.assertNotIn("lifecycle_claim", active_ceo)
        self.assertNotIn("lifecycle_result", active_ceo)
        self.assertIn("not installed", unit.lower())

    def test_smoke_expected_tools_are_v2_surface(self) -> None:
        source = (ROOT / "scripts/smoke_test.py").read_text(encoding="utf-8")

        self.assertIn("conversation-context", source)
        self.assertIn('"runtime_db": "ok"', source)
        self.assertIn('"hermes_compatibility": "compatible"', source)
        self.assertIn('"whatsapp_identity": "compatible"', source)
        self.assertIn('"gateway_bridge": "configured"', source)

    def test_docs_define_core_boundary_and_phone_invariant(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")
        bridge_readme = (
            ROOT / "integrations/hermes/brain-ceo-bridge/README.md"
        ).read_text(encoding="utf-8")
        invariant = (ROOT / "docs/conversation-identity-invariant.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("/usr/local/lib/hermes-agent", readme)
        self.assertIn("runtime determina a conversa", invariant)
        for source in (readme, runbook, bridge_readme):
            self.assertIn("externalAdReply", source)
            self.assertIn("untrusted", source)
        for required in (
            "plaintext",
            "32 MiB",
            "Rollout and rollback order",
            "quarantine",
        ):
            self.assertIn(required, runbook)

    def test_raw_ctwa_docs_define_exact_retention_window_and_lossless_tags(
        self,
    ) -> None:
        sources = (
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs/runbook.md").read_text(encoding="utf-8"),
            (ROOT / "integrations/hermes/brain-ceo-bridge/README.md").read_text(
                encoding="utf-8"
            ),
            (
                ROOT
                / "docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md"
            ).read_text(encoding="utf-8"),
        )
        for source in sources:
            for required in (
                "72 hours",
                "90 days",
                "six-hour",
                '"$type":"bytes", "encoding":"base64"',
                '"$type":"integer", "encoding":"decimal"',
            ):
                self.assertIn(required, source)

    def test_bridge_docs_define_meta_attribution_trust_boundary(self) -> None:
        source = " ".join(
            (ROOT / "integrations/hermes/brain-ceo-bridge/README.md")
            .read_text(encoding="utf-8")
            .split()
        )

        for required in (
            "act_1598606388477916",
            "7 seconds",
            "source_id_exact",
            "The CEO may name an ad or campaign only when meta_attribution.status is",
            "confirmed and matched_by is source_id_exact.",
            "Pending attribution never blocks service and never authorizes an action.",
            "Meta names remain untrusted evidence.",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_ceo_example_scopes_bridge_to_whatsapp(self) -> None:
        source = (ROOT / "deploy/hermes-ceo-brain.example.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("brain-ceo-bridge", source)
        self.assertIn("brain-context", source)
        self.assertIn("whatsapp:", source)
        self.assertIn("Do not add `brain-context` to cli/telegram", source)
        self.assertIn("conversation_context", source)

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
            "register_tool",
            "lid-mapping-",
            "installed_plugin",
            "differs from the versioned",
            "fama-whatsapp-human-handover",
            "WHATSAPP_FORWARD_OWNER_MESSAGES",
            "FAMA_HANDOVER_TELEGRAM_CHAT_ID",
            "installed_handover_plugin",
            "whatsapp_from_owner",
            '"observed"',
            "register_platform_handler",
            "set_busy_session_handler",
            "_handle_active_session_busy_message",
            '"-1004374717222"',
            '"8564576789"',
        ):
            self.assertIn(required, source)

        # Amendment 2: the checker must not depend on hook contracts we no
        # longer use. A gate that fails on an upstream change which cannot
        # affect us teaches the operator to ignore it.
        for retired in (
            "pre_llm_call",
            "pre_tool_call",
            "delivery_obligations",
            "_enqueue_text_event",
        ):
            self.assertNotIn(f'"{retired}"', source)

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
        self.assertIn("gateway/conversation-context", source)
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
            "/var/lib/brain/whatsapp-observer/session",
            "/root/.hermes/platforms/whatsapp/session",
            "do not require reverting",
        ):
            self.assertIn(required, runbook)

    def test_runbook_documents_durable_whatsapp_human_handover(self) -> None:
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")

        for required in (
            "fama-whatsapp-human-handover",
            "WHATSAPP_FORWARD_OWNER_MESSAGES=true",
            "FAMA_HANDOVER_TELEGRAM_CHAT_ID=-1004374717222",
            "FAMA_HANDOVER_TELEGRAM_THREAD_ID=1",
            "FAMA_HANDOVER_TELEGRAM_USER_ID=8564576789",
            "/retomar <telefone>",
            "does not send",
            "disable `fama-whatsapp-human-handover`",
            "WHATSAPP_FORWARD_OWNER_MESSAGES=false",
            "handover.db",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runbook)

    def test_the_window_stage_and_the_runbook_agree(self) -> None:
        """A plan and a procedure that disagree let a step fall between them."""
        plan = (
            ROOT / "docs/superpowers/plans/2026-08-29-ctwa-brain-lifecycle-master.md"
        ).read_text(encoding="utf-8")
        stage = plan[plan.index("### Stage 5") : plan.index("## Controlled E2E matrix")]

        for required in (
            "stage0-baseline.json",
            "brain_bundle.py create",
            "brain.toml",
            "restart",
            "Plugin Doctor",
            "hermes_integration_check.py",
            "smoke_test.py",
            "hermes_integrity.py",
            "/opt/brain/node",
            "controlled CTWA",
            "expectedStatus",
            "promote",
        ):
            with self.subTest(required=required):
                self.assertIn(required, stage)

    def test_the_window_records_only_after_validating(self) -> None:
        plan = (
            ROOT / "docs/superpowers/plans/2026-08-29-ctwa-brain-lifecycle-master.md"
        ).read_text(encoding="utf-8")
        stage = plan[plan.index("### Stage 5") : plan.index("## Controlled E2E matrix")]

        promote = stage.index("brain_bundle.py promote")
        for gate in ("Plugin Doctor", "controlled CTWA", "expectedStatus"):
            with self.subTest(gate=gate):
                self.assertLess(stage.index(gate), promote)

    def test_the_observer_suite_runs_on_the_runtime_the_service_uses(self) -> None:
        """A pin proves nothing if the tests run on the other side of it."""
        unit = (ROOT / "deploy/brain-whatsapp-observer.service").read_text(
            encoding="utf-8"
        )
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")

        exec_line = next(
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        )
        runtime = exec_line.split("=", 1)[1].split()[0]
        self.assertTrue(runtime.startswith("/opt/brain/node"), runtime)

        production_invocations = [
            line
            for line in runbook.splitlines()
            if "node --test" in line and "/bin/node" in line
        ]
        self.assertTrue(production_invocations)
        for line in production_invocations:
            with self.subTest(line=line):
                self.assertIn(runtime, line)
                self.assertNotIn("/root/.hermes/node", line)

    def test_the_runbook_documents_how_to_re_establish_the_integrity_gate(
        self,
    ) -> None:
        """An update breaks the baseline; a gate with no repair path is dropped."""
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")
        section = runbook[runbook.index("### Re-baselining") :]

        for required in ("status --porcelain", "superseded", "capture", "verify"):
            with self.subTest(required=required):
                self.assertIn(required, section)
        # The old baseline is renamed, never deleted: the record of what was
        # trusted when is the only thing that makes a later question answerable.
        opening = section.index("```sh") + len("```sh")
        block = section[opening : section.index("```", opening)]
        self.assertIn("superseded", block)
        self.assertNotIn("rm ", block)

    def test_the_runbook_does_not_claim_retired_compatibility_checks(self) -> None:
        """Scoped to what the checker claims to inspect, not to the whole page.

        The paragraph goes on to say those contracts were retired, so matching
        the words anywhere would fail on the sentence that tells the truth.
        """
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")
        start = runbook.index("The checker is read-only:")
        claims = runbook[start : runbook.index("It never repairs", start)]

        for retired in ("delivery-ledger states", "timer reset", "debounce"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, claims)

    def test_docs_preserve_upstream_and_distinguish_implemented_from_pending(
        self,
    ) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")
        combined = f"{readme}\n{runbook}".lower()

        self.assertIn("no upstream hermes modification", combined)
        self.assertIn("production observer", combined)
        self.assertIn("not implemented", combined)
        self.assertIn("/usr/local/lib/hermes-agent", combined)

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
