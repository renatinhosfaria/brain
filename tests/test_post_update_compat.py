from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES = Path("/usr/local/lib/hermes-agent")


def load_smoke():
    spec = importlib.util.spec_from_file_location(
        "brain_smoke_test", ROOT / "scripts/smoke_test.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HealthContractTests(unittest.TestCase):
    def setUp(self):
        self.health = {
            "status": "ok",
            "hermes_state_db": "ok",
            "hermes_kanban_db": "ok",
            "runtime_db": "ok",
            "whatsapp_identity": "compatible",
            "gateway_bridge": "configured",
            "schema": "compatible",
            "hermes_compatibility": "compatible",
        }

    def test_optional_health_extension_does_not_break_compatibility(self):
        self.health["meta_ads_mcp"] = "disabled"
        self.assertTrue(load_smoke().compatible_health(self.health))

    def test_required_health_failure_and_missing_field_are_rejected(self):
        for field in self.health:
            with self.subTest(field=field):
                changed = {**self.health, field: "incompatible"}
                self.assertFalse(load_smoke().compatible_health(changed))
                missing = dict(self.health)
                del missing[field]
                self.assertFalse(load_smoke().compatible_health(missing))
        self.assertFalse(load_smoke().compatible_health([]))


@unittest.skipUnless((HERMES / "venv/bin/python").exists(), "requires installed Hermes")
class InstalledHermesContractTests(unittest.TestCase):
    def test_current_hermes_supports_brain_runtime_contracts(self):
        # Fresh process and a temporary home: never load production credentials or plugins.
        script = """
import importlib.util
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location('checker', sys.argv[2])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.check_upstream_contracts(Path(sys.argv[1]))
from unittest.mock import patch
from hermes_cli import kanban_db
with patch.object(kanban_db, '_board_meta_for', return_value={'project_id': 'production-project'}):
    with patch('hermes_cli.projects_db.connect_closing', side_effect=AssertionError('probe reached production projects')) as projects:
        m.check_trusted_subscription()
        projects.assert_not_called()
"""
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [
                    str(HERMES / "venv/bin/python"),
                    "-c",
                    script,
                    str(HERMES),
                    str(ROOT / "scripts/hermes_integration_check.py"),
                ],
                env={**os.environ, "HERMES_HOME": home},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
