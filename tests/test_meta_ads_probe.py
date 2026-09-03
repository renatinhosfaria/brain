from __future__ import annotations

import contextlib
import importlib
import io
import unittest
from unittest.mock import patch

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.meta_ads_models import MetaAdsError


def _settings(*, enabled: bool) -> BrainSettings:
    return BrainSettings(
        principals={
            "default": PrincipalConfig(
                "default", "gateway", token_digest("gateway"), frozenset({"conversation_context"})
            )
        },
        cursor_secret=b"c" * 32,
        transport_hmac_secret=b"t" * 32,
        meta_ads_mcp_enabled=enabled,
        meta_ads_mcp_api_key="probe-secret",
    )


class ProbeTests(unittest.TestCase):
    def _run(self, settings: BrainSettings, probe_error: Exception | None = None):
        module = importlib.import_module("scripts.meta_ads_mcp_probe")

        class FakeClient:
            def __init__(self, _settings):
                pass

            def probe(self):
                if probe_error:
                    raise probe_error

            def close(self):
                pass

        output = io.StringIO()
        with patch.object(module.BrainSettings, "from_env", return_value=settings), patch.object(
            module, "RemoteMetaAdsMcpClient", FakeClient
        ), contextlib.redirect_stdout(output):
            code = module.main([])
        return code, output.getvalue()

    def test_disabled_prints_only_disabled_and_succeeds(self):
        code, output = self._run(_settings(enabled=False))
        self.assertEqual((code, output), (0, "disabled\n"))

    def test_enabled_probe_prints_exact_account(self):
        code, output = self._run(_settings(enabled=True))
        self.assertEqual((code, output), (0, "ready account=act_1598606388477916\n"))

    def test_failure_prints_bounded_code_and_nonzero(self):
        code, output = self._run(_settings(enabled=True), MetaAdsError("meta_auth_unavailable"))
        self.assertEqual((code, output), (1, "error meta_auth_unavailable\n"))
        self.assertNotIn("probe-secret", output)

    def test_cli_rejects_arguments(self):
        module = importlib.import_module("scripts.meta_ads_mcp_probe")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = module.main(["--api-key", "secret"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "error invalid_arguments\n")
