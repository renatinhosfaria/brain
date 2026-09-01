from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.install_brain_secrets import restore_files, snapshot_files

ROOT = Path(__file__).parents[1]


class DeploymentContractTests(unittest.TestCase):
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
