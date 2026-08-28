from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).parents[1] / "integrations/hermes/brain-ceo-bridge"
PLUGIN_MODULE_NAME = "brain_ceo_bridge_test"


class _Context:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


class CEOBridgePluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context_values: dict[str, ContextVar[str]] = {
            name: ContextVar(name, default="")
            for name in (
                "HERMES_SESSION_PLATFORM",
                "HERMES_SESSION_CHAT_TYPE",
                "HERMES_SESSION_CHAT_ID",
                "HERMES_SESSION_KEY",
                "HERMES_SESSION_ID",
                "HERMES_SESSION_PROFILE",
            )
        }
        gateway = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")

        def get_session_env(name: str, default: str = "") -> str:
            return self.context_values.get(
                name, ContextVar(name, default=default)
            ).get()

        session_context.get_session_env = get_session_env  # type: ignore[attr-defined]
        gateway.session_context = session_context  # type: ignore[attr-defined]
        self.gateway_modules = {
            "gateway": sys.modules.get("gateway"),
            "gateway.session_context": sys.modules.get("gateway.session_context"),
        }
        sys.modules["gateway"] = gateway
        sys.modules["gateway.session_context"] = session_context

        init_file = PLUGIN_DIR / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            PLUGIN_MODULE_NAME,
            init_file,
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        if spec is None or spec.loader is None:
            raise AssertionError("plugin import spec could not be created")
        self.plugin = importlib.util.module_from_spec(spec)
        sys.modules[PLUGIN_MODULE_NAME] = self.plugin
        spec.loader.exec_module(self.plugin)
        self.tools = sys.modules[f"{PLUGIN_MODULE_NAME}.tools"]

    def tearDown(self) -> None:
        sys.modules.pop(PLUGIN_MODULE_NAME, None)
        for name, module in self.gateway_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        for variable in self.context_values.values():
            variable.set("")

    @staticmethod
    def context_values_for(prefix: str) -> dict[str, str]:
        return {
            "HERMES_SESSION_PLATFORM": "whatsapp",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_CHAT_ID": f"{prefix}@lid",
            "HERMES_SESSION_KEY": f"wa:{prefix}",
            "HERMES_SESSION_ID": prefix,
            "HERMES_SESSION_PROFILE": "default",
        }

    def bind_context(self, values: dict[str, str]) -> list:
        return [self.context_values[name].set(value) for name, value in values.items()]

    def test_plugin_registers_only_zero_argument_tool(self) -> None:
        context = _Context()

        self.plugin.register(context)

        self.assertEqual(
            [call["name"] for call in context.calls], ["conversation_phone"]
        )
        self.assertEqual(context.calls[0]["toolset"], "brain-context")
        self.assertEqual(
            context.calls[0]["schema"]["parameters"],
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
        self.assertEqual(context.calls[0]["requires_env"], ["BRAIN_GATEWAY_TOKEN"])

    def test_handler_accepts_implicit_default_profile_and_calls_fixed_endpoint(
        self,
    ) -> None:
        values = self.context_values_for("implicit")
        values["HERMES_SESSION_PROFILE"] = ""
        self.bind_context(values)
        requests: list[tuple[str, dict]] = []

        def opener(request, timeout):
            requests.append(
                (request.full_url, {**json.loads(request.data), "timeout": timeout})
            )
            return _Response(b'{"status":"ok","phone":"5534999772714"}')

        with patch.object(self.tools.urllib.request, "urlopen", opener):
            result = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(result, {"status": "ok", "phone": "5534999772714"})
        self.assertEqual(
            requests[0][0], "http://127.0.0.1:8765/internal/gateway/conversation-phone"
        )
        self.assertEqual(
            requests[0][1],
            {
                "platform": "whatsapp",
                "chat_type": "dm",
                "chat_id": "implicit@lid",
                "session_key": "wa:implicit",
                "session_id": "implicit",
                "timeout": 5.0,
            },
        )

    def test_handler_allows_observed_latency_budget_without_sleep(self) -> None:
        self.bind_context(self.context_values_for("latency"))
        timeouts: list[float] = []

        def opener(_request, timeout):
            timeouts.append(timeout)
            if timeout < 2.52:
                raise TimeoutError
            return _Response(b'{"status":"ok","phone":"5534999772714"}')

        with patch.object(self.tools.urllib.request, "urlopen", opener):
            result = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(result, {"status": "ok", "phone": "5534999772714"})
        self.assertGreaterEqual(timeouts[0], 2.52)

    def test_handler_accepts_explicit_default_profile_and_attempts_post(self) -> None:
        self.bind_context(self.context_values_for("explicit"))

        with patch.object(
            self.tools.urllib.request,
            "urlopen",
            return_value=_Response(
                b'{"status":"unavailable","reason":"phone_not_resolved"}'
            ),
        ) as opener:
            result = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(
            result, {"status": "unavailable", "reason": "phone_not_resolved"}
        )
        opener.assert_called_once()

    def test_handler_rejects_named_profile_without_network(self) -> None:
        values = self.context_values_for("named")
        values["HERMES_SESSION_PROFILE"] = "porteiro"
        self.assert_unavailable_without_post(values)

    def test_handler_rejects_empty_required_fields_without_network(self) -> None:
        for field in (
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_KEY",
            "HERMES_SESSION_ID",
        ):
            with self.subTest(field=field):
                values = self.context_values_for(field.removeprefix("HERMES_SESSION_"))
                values[field] = ""
                self.assert_unavailable_without_post(values)

    def test_handler_rejects_wrong_platform_without_network(self) -> None:
        values = self.context_values_for("telegram")
        values["HERMES_SESSION_PLATFORM"] = "telegram"
        self.assert_unavailable_without_post(values)

    def test_handler_rejects_wrong_chat_type_without_network(self) -> None:
        values = self.context_values_for("group")
        values["HERMES_SESSION_CHAT_TYPE"] = "group"
        self.assert_unavailable_without_post(values)

    def test_handler_rejects_nonempty_args_and_invalid_context(self) -> None:
        self.bind_context(self.context_values_for("a"))
        opener = lambda *_args, **_kwargs: self.fail("network must not be called")

        with patch.object(self.tools.urllib.request, "urlopen", opener):
            extra_args = json.loads(
                self.tools.conversation_phone(
                    {"chat_id": "forged"}, BRAIN_GATEWAY_TOKEN="gateway-secret"
                )
            )
            self.context_values["HERMES_SESSION_PLATFORM"].set("telegram")
            wrong_platform = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(
            extra_args, {"status": "unavailable", "reason": "phone_not_resolved"}
        )
        self.assertEqual(
            wrong_platform, {"status": "unavailable", "reason": "phone_not_resolved"}
        )

    def assert_unavailable_without_post(self, values: dict[str, str]) -> None:
        self.bind_context(values)
        opener = lambda *_args, **_kwargs: self.fail("network must not be called")

        with patch.object(self.tools.urllib.request, "urlopen", opener):
            result = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(
            result, {"status": "unavailable", "reason": "phone_not_resolved"}
        )

    def test_handler_returns_unavailable_for_missing_secret_timeout_or_bad_response(
        self,
    ) -> None:
        self.bind_context(self.context_values_for("a"))
        no_secret = json.loads(self.tools.conversation_phone({}))
        self.assertEqual(no_secret["status"], "unavailable")

        with patch.object(
            self.tools.urllib.request, "urlopen", side_effect=TimeoutError
        ):
            timeout = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )
        with patch.object(
            self.tools.urllib.request,
            "urlopen",
            return_value=_Response(b"not-json"),
        ):
            malformed = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(
            timeout, {"status": "unavailable", "reason": "phone_not_resolved"}
        )
        self.assertEqual(
            malformed, {"status": "unavailable", "reason": "phone_not_resolved"}
        )

    def test_handler_rejects_oversized_response(self) -> None:
        self.bind_context(self.context_values_for("a"))
        payload = b'{"status":"ok","phone":"5534999772714"}' + (b" " * 16_385)

        with patch.object(
            self.tools.urllib.request, "urlopen", return_value=_Response(payload)
        ):
            result = json.loads(
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            )

        self.assertEqual(
            result, {"status": "unavailable", "reason": "phone_not_resolved"}
        )

    def test_parallel_contextvars_stay_paired(self) -> None:
        seen: list[dict[str, str]] = []
        lock = threading.Lock()

        def opener(request, timeout):
            del timeout
            time.sleep(0.001)
            with lock:
                seen.append(json.loads(request.data))
            return _Response(b'{"status":"unavailable","reason":"phone_not_resolved"}')

        async def run() -> None:
            for _ in range(50):
                await asyncio.gather(
                    asyncio.to_thread(call_sync, "a"),
                    asyncio.to_thread(call_sync, "b"),
                )

        def call_sync(prefix: str) -> None:
            tokens = self.bind_context(self.context_values_for(prefix))
            try:
                self.tools.conversation_phone({}, BRAIN_GATEWAY_TOKEN="gateway-secret")
            finally:
                for name, token in zip(self.context_values, tokens):
                    self.context_values[name].reset(token)

        with patch.object(self.tools.urllib.request, "urlopen", opener):
            asyncio.run(run())

        self.assertEqual(len(seen), 100)
        for body in seen:
            prefix = body["chat_id"].split("@", 1)[0]
            self.assertEqual(body["session_key"], f"wa:{prefix}")
            self.assertEqual(body["session_id"], prefix)


if __name__ == "__main__":
    unittest.main()
