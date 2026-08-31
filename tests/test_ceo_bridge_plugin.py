from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).parents[1] / "integrations/hermes/brain-ceo-bridge"
PLUGIN_MODULE_NAME = "brain_ceo_bridge_test"

SESSION_FIELDS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PROFILE",
)


class _Context:
    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.hooks: dict[str, object] = {}

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_hook(self, name, handler) -> None:
        self.hooks[name] = handler


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload, separators=(",", ":")).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


class CEOBridgePluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = patch.dict(
            os.environ, {"BRAIN_GATEWAY_TOKEN": "gateway-secret"}
        )
        self.env_patch.start()
        self.context_values = {
            name: ContextVar(name, default="") for name in SESSION_FIELDS
        }
        gateway = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")

        def get_session_env(name: str, default: str = "") -> str:
            variable = self.context_values.get(name)
            return variable.get() if variable is not None else default

        session_context.get_session_env = get_session_env  # type: ignore[attr-defined]
        gateway.session_context = session_context  # type: ignore[attr-defined]
        self.old_gateway_modules = {
            "gateway": sys.modules.get("gateway"),
            "gateway.session_context": sys.modules.get("gateway.session_context"),
        }
        sys.modules["gateway"] = gateway
        sys.modules["gateway.session_context"] = session_context

        spec = importlib.util.spec_from_file_location(
            PLUGIN_MODULE_NAME,
            PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        if spec is None or spec.loader is None:
            raise AssertionError("plugin import spec could not be created")
        self.plugin = importlib.util.module_from_spec(spec)
        sys.modules[PLUGIN_MODULE_NAME] = self.plugin
        spec.loader.exec_module(self.plugin)
        self.tools_module = sys.modules[f"{PLUGIN_MODULE_NAME}.tools"]
        self.ctx = _Context()
        self.plugin.register(self.ctx)
        self.bind_context(self.valid_context())

    def tearDown(self) -> None:
        self.env_patch.stop()
        for name in list(sys.modules):
            if name == PLUGIN_MODULE_NAME or name.startswith(f"{PLUGIN_MODULE_NAME}."):
                sys.modules.pop(name, None)
        for name, module in self.old_gateway_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    # ------------------------------------------------------------------

    @staticmethod
    def valid_context() -> dict[str, str]:
        return {
            "HERMES_SESSION_PLATFORM": "whatsapp",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_CHAT_ID": "123456789012345@lid",
            "HERMES_SESSION_KEY": "wa:g",
            "HERMES_SESSION_ID": "g-one",
            "HERMES_SESSION_PROFILE": "default",
        }

    def bind_context(self, values: dict[str, str]) -> None:
        for name, value in values.items():
            self.context_values[name].set(value)

    @staticmethod
    def ok_payload(**overrides) -> dict:
        payload = {
            "status": "ok",
            "contact": {
                "phone_e164": "5534999772714",
                "display_name": "Maria Silva",
                "display_name_source": "whatsapp_profile",
            },
            "events": [
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ctwa_candidate",
                    "source_app": "instagram",
                    "inbound_kind": None,
                }
            ],
        }
        payload.update(overrides)
        return payload

    def call_tool(self, payload: object, args: dict | None = None) -> dict:
        seen: list[dict] = []

        def opener(request, timeout):
            seen.append(
                {"url": request.full_url, "body": json.loads(request.data)}
            )
            return _Response(payload)

        with patch.object(self.tools_module.urllib.request, "urlopen", opener):
            raw = self.tools_module.conversation_context(
                {} if args is None else args, token="gateway-secret"
            )
        self.requests = seen
        return json.loads(raw)

    # ------------------------------------------------------------------

    def test_registers_one_tool_and_no_hooks(self) -> None:
        """Amendment 2 removed every hook; none may come back by accident."""
        self.assertEqual([tool["name"] for tool in self.ctx.tools], [
            "conversation_context"
        ])
        self.assertEqual(self.ctx.hooks, {})
        self.assertEqual(self.ctx.tools[0]["toolset"], "brain-context")
        self.assertEqual(
            self.ctx.tools[0]["schema"]["parameters"]["properties"], {}
        )
        self.assertIs(
            self.ctx.tools[0]["schema"]["parameters"]["additionalProperties"], False
        )

    def test_module_exposes_no_hook_callables(self) -> None:
        for removed in ("pre_gateway_dispatch", "pre_llm_call", "pre_tool_call"):
            with self.subTest(hook=removed):
                self.assertFalse(hasattr(self.tools_module, removed))

    def test_returns_brain_contract_for_the_current_dm(self) -> None:
        result = self.call_tool(self.ok_payload())

        self.assertEqual(result, self.ok_payload())
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0]["url"].endswith("/conversation-context"))
        self.assertEqual(
            self.requests[0]["body"],
            {
                "platform": "whatsapp",
                "chat_type": "dm",
                "chat_id": "123456789012345@lid",
                "session_key": "wa:g",
                "session_id": "g-one",
            },
        )

    def test_request_carries_no_turn_identifier(self) -> None:
        self.call_tool(self.ok_payload())

        self.assertNotIn("wa_turn_id", self.requests[0]["body"])
        self.assertNotIn("turn_id", self.requests[0]["body"])

    def test_passes_through_brains_own_unavailable_reason(self) -> None:
        result = self.call_tool(
            {"status": "unavailable", "reason": "no_recent_transport"}
        )

        self.assertEqual(
            result, {"status": "unavailable", "reason": "no_recent_transport"}
        )

    def test_rejects_a_payload_that_is_not_exactly_the_contract(self) -> None:
        for label, payload in (
            ("extra field", self.ok_payload(turn={"wa_turn_id": "waturn_x"})),
            ("no events", self.ok_payload(events=[])),
            ("bad phone", self.ok_payload(contact={
                "phone_e164": "not-a-phone",
                "display_name": None,
                "display_name_source": None,
            })),
            ("named without source", self.ok_payload(contact={
                "phone_e164": "5534999772714",
                "display_name": "Maria",
                "display_name_source": None,
            })),
            ("asserted inbound_kind", self.ok_payload(events=[{
                "event_id": "waevt_safe",
                "transport_kind": "ctwa_candidate",
                "source_app": "instagram",
                "inbound_kind": "ctwa_first_contact",
            }])),
            ("unknown transport kind", self.ok_payload(events=[{
                "event_id": "waevt_safe",
                "transport_kind": "something_new",
                "source_app": None,
                "inbound_kind": None,
            }])),
        ):
            with self.subTest(label):
                self.assertEqual(
                    self.call_tool(payload),
                    {"status": "unavailable", "reason": "context_unavailable"},
                )

    def test_rejects_any_argument(self) -> None:
        """The schema takes none, so an argument means a caller misunderstood."""
        result = self.call_tool(self.ok_payload(), args={"phone": "5534999772714"})

        self.assertEqual(
            result, {"status": "unavailable", "reason": "context_unavailable"}
        )

    def test_ignores_scopes_that_are_not_the_ceo_whatsapp_dm(self) -> None:
        for label, override in (
            ("group", {"HERMES_SESSION_CHAT_TYPE": "group"}),
            ("telegram", {"HERMES_SESSION_PLATFORM": "telegram"}),
            ("worker profile", {"HERMES_SESSION_PROFILE": "reno"}),
            ("no chat id", {"HERMES_SESSION_CHAT_ID": ""}),
        ):
            with self.subTest(label):
                self.bind_context({**self.valid_context(), **override})
                self.assertEqual(
                    self.call_tool(self.ok_payload()),
                    {"status": "unavailable", "reason": "context_unavailable"},
                )
                self.assertEqual(self.requests, [])

    def test_a_failing_brain_never_raises_into_hermes(self) -> None:
        def exploding(request, timeout):
            raise TimeoutError("timed out")

        with patch.object(self.tools_module.urllib.request, "urlopen", exploding):
            raw = self.tools_module.conversation_context({}, token="gateway-secret")

        self.assertEqual(
            json.loads(raw),
            {"status": "unavailable", "reason": "context_unavailable"},
        )

    def test_an_oversized_response_is_refused(self) -> None:
        huge = self.ok_payload(events=[
            {
                "event_id": f"waevt_{index}",
                "transport_kind": "ordinary_inbound",
                "source_app": "x" * 400,
                "inbound_kind": None,
            }
            for index in range(200)
        ])

        self.assertEqual(
            self.call_tool(huge),
            {"status": "unavailable", "reason": "context_unavailable"},
        )

    def test_missing_token_does_not_reach_brain(self) -> None:
        reached = False

        def opener(request, timeout):
            nonlocal reached
            reached = True
            return _Response(self.ok_payload())

        with patch.dict(os.environ, {}, clear=True), patch.object(
            self.tools_module.urllib.request, "urlopen", opener
        ):
            raw = self.tools_module.conversation_context({})

        self.assertFalse(reached)
        self.assertEqual(
            json.loads(raw),
            {"status": "unavailable", "reason": "context_unavailable"},
        )


if __name__ == "__main__":
    unittest.main()
