from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from contextvars import ContextVar, copy_context
from pathlib import Path
from threading import Thread
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).parents[1] / "integrations/hermes/brain-ceo-bridge"
PLUGIN_MODULE_NAME = "brain_ceo_bridge_test"


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
    def brain_response(url: str) -> _Response:
        if url.endswith("/turn-register"):
            return _Response(
                {
                    "status": "ok",
                    "wa_turn_id": "waturn_current",
                    "correlation": "correlated",
                }
            )
        return _Response(
            {
                "status": "ok",
                "contact": {
                    "phone_e164": "5534999772714",
                    "display_name": "Maria Silva",
                    "display_name_source": "whatsapp_profile",
                },
                "turn": {"wa_turn_id": "waturn_current"},
                "events": [
                    {
                        "event_id": "waevt_safe",
                        "transport_kind": "ctwa_candidate",
                        "source_app": "instagram",
                        "inbound_kind": None,
                    }
                ],
            }
        )

    @staticmethod
    def inbound_event(
        message_id: str = "3EB0AAA",
        *,
        platform: str = "whatsapp",
        chat_type: str = "dm",
        chat_id: str = "123456789012345@lid",
        profile: str | None = None,
        internal: bool = False,
        plain_platform: bool = False,
    ) -> types.SimpleNamespace:
        """Mirror the MessageEvent shape gateway/platforms/base.py defines."""
        return types.SimpleNamespace(
            message_id=message_id,
            internal=internal,
            text="oi",
            source=types.SimpleNamespace(
                platform=platform
                if plain_platform
                else types.SimpleNamespace(value=platform),
                chat_id=chat_id,
                chat_type=chat_type,
                profile=profile,
            ),
        )

    def dispatch(self, *events) -> None:
        for event in events:
            self.assertIsNone(self.ctx.hooks["pre_gateway_dispatch"](event=event))

    def register_turn(self, turn_id: str, requests: list) -> None:
        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return self.brain_response(request.full_url)

        with patch.object(self.tools_module.urllib.request, "urlopen", opener):
            self.ctx.hooks["pre_llm_call"](turn_id=turn_id, user_message="oi")

    def test_dispatch_buffer_feeds_message_ids_into_registration(self) -> None:
        requests: list[dict] = []
        self.dispatch(self.inbound_event("3EB0AAA"), self.inbound_event("3EB0BBB"))

        self.register_turn("opaque-turn", requests)

        self.assertEqual(requests[0]["message_ids"], ["3EB0AAA", "3EB0BBB"])

    def test_dispatch_buffer_drains_once_per_turn(self) -> None:
        requests: list[dict] = []
        self.dispatch(self.inbound_event("3EB0AAA"))

        self.register_turn("turn-one", requests)
        self.register_turn("turn-two", requests)

        self.assertEqual(requests[0]["message_ids"], ["3EB0AAA"])
        self.assertEqual(requests[1]["message_ids"], [])

    def test_dispatch_ignores_internal_and_foreign_scopes(self) -> None:
        requests: list[dict] = []
        self.dispatch(
            self.inbound_event("3EB0INTERNAL", internal=True),
            self.inbound_event("3EB0TELEGRAM", platform="telegram"),
            self.inbound_event("3EB0GROUP", chat_type="group"),
            self.inbound_event("3EB0OTHERPROFILE", profile="reno"),
            self.inbound_event("3EB0OTHERCHAT", chat_id="999@lid"),
        )

        self.register_turn("opaque-turn", requests)

        self.assertEqual(requests[0]["message_ids"], [])

    def test_stage_cards_share_the_origin_turn_across_notification_turns(self) -> None:
        """The P7 regression, in the exact production sequence."""
        requests: list[dict] = []
        self.dispatch(self.inbound_event("3EB0AAA"))
        self.register_turn("external-turn", requests)
        porteiro = self.ctx.hooks["pre_tool_call"](
            tool_name="kanban_create",
            args={"assignee": "porteiro"},
            turn_id="external-turn",
        )

        # Two Kanban-completion turns follow, each with an empty buffer.
        self.register_turn("kanban-notification-1", requests)
        cadastro = self.ctx.hooks["pre_tool_call"](
            tool_name="kanban_create",
            args={"assignee": "cadastro"},
            turn_id="kanban-notification-1",
        )
        self.register_turn("kanban-notification-2", requests)
        reno = self.ctx.hooks["pre_tool_call"](
            tool_name="kanban_create",
            args={"assignee": "reno"},
            turn_id="kanban-notification-2",
        )

        self.assertEqual(
            porteiro["args"]["idempotency_key"], "whatsapp:waturn_current:porteiro"
        )
        self.assertEqual(
            cadastro["args"]["idempotency_key"], "whatsapp:waturn_current:cadastro"
        )
        self.assertEqual(
            reno["args"]["idempotency_key"], "whatsapp:waturn_current:reno"
        )

    def test_pre_tool_leaves_key_untouched_without_a_retained_origin(self) -> None:
        """A wrong binding is silent and permanent; an unrewritten key is not."""
        result = self.ctx.hooks["pre_tool_call"](
            tool_name="kanban_create",
            args={"assignee": "cadastro", "idempotency_key": "model-supplied"},
            turn_id="kanban-notification-1",
        )
        self.assertIsNone(result)

    def test_dispatch_accepts_enum_or_plain_platform(self) -> None:
        """Not every path is guaranteed to pass a Platform member."""
        requests: list[dict] = []
        self.dispatch(self.inbound_event("3EB0PLAIN", plain_platform=True))

        self.register_turn("opaque-turn", requests)

        self.assertEqual(requests[0]["message_ids"], ["3EB0PLAIN"])

    def test_dispatch_hook_performs_no_io(self) -> None:
        """Premise P4: this hook runs unbounded upstream, so it must not block."""
        source = (PLUGIN_DIR / "tools.py").read_text(encoding="utf-8")
        start = source.index("def pre_gateway_dispatch(")
        end = source.index("def pre_llm_call(")
        body = source[start:end]
        for forbidden in (
            "urlopen",
            "_post(",
            "open(",
            "sleep",
            "subprocess",
            "Request(",
            "socket",
        ):
            self.assertNotIn(forbidden, body)

    def test_registers_one_zero_arg_context_tool_and_official_hooks(self) -> None:
        self.assertEqual(
            [tool["name"] for tool in self.ctx.tools], ["conversation_context"]
        )
        self.assertEqual(self.ctx.tools[0]["toolset"], "brain-context")
        self.assertEqual(
            self.ctx.tools[0]["schema"]["parameters"],
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        self.assertEqual(
            set(self.ctx.hooks),
            {"pre_gateway_dispatch", "pre_llm_call", "pre_tool_call"},
        )
        self.assertEqual(self.ctx.tools[0]["requires_env"], ["BRAIN_GATEWAY_TOKEN"])

        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PLUGIN_DIR / "__init__.py", PLUGIN_DIR / "tools.py")
        )
        self.assertNotIn("monkeypatch", source.casefold())
        self.assertNotIn("preload", source.casefold())
        self.assertNotIn("hermes_cli", source)
        self.assertNotIn("/usr/local/lib/hermes-agent", source)

    def test_pre_llm_registers_default_whatsapp_dm(self) -> None:
        requests: list[tuple[str, dict, str]] = []

        def opener(request, timeout):
            requests.append(
                (request.full_url, json.loads(request.data), request.method)
            )
            self.assertEqual(timeout, 5.0)
            return self.brain_response(request.full_url)

        with patch.object(self.tools_module.urllib.request, "urlopen", opener):
            result = self.ctx.hooks["pre_llm_call"](
                turn_id="opaque-turn", user_message="raw transient body"
            )

        self.assertIsNone(result)
        self.assertEqual(requests[0][2], "POST")
        self.assertTrue(requests[0][0].endswith("/internal/gateway/turn-register"))
        self.assertEqual(requests[0][1]["turn_id"], "opaque-turn")
        self.assertEqual(requests[0][1]["user_message"], "raw transient body")
        self.assertIsInstance(requests[0][1]["turn_timestamp"], float)

    def test_pre_llm_ignores_other_scopes(self) -> None:
        for field, value in (
            ("HERMES_SESSION_PROFILE", "reno"),
            ("HERMES_SESSION_PLATFORM", "telegram"),
            ("HERMES_SESSION_CHAT_TYPE", "group"),
        ):
            with self.subTest(field=field):
                values = self.valid_context()
                values[field] = value
                self.bind_context(values)
                with patch.object(
                    self.tools_module.urllib.request, "urlopen"
                ) as opener:
                    self.ctx.hooks["pre_llm_call"](
                        turn_id="opaque-turn", user_message="body"
                    )
                opener.assert_not_called()

    def test_context_tool_uses_current_registered_turn_and_safe_contract(self) -> None:
        requests: list[dict] = []

        def opener(request, timeout):
            del timeout
            requests.append(json.loads(request.data))
            return self.brain_response(request.full_url)

        with patch.object(self.tools_module.urllib.request, "urlopen", opener):
            self.ctx.hooks["pre_llm_call"](
                turn_id="opaque-turn", user_message="raw transient body"
            )
            result = json.loads(
                self.tools_module.conversation_context(
                    {}, BRAIN_GATEWAY_TOKEN="gateway-secret"
                )
            )

        self.assertEqual(result["events"][0]["transport_kind"], "ctwa_candidate")
        self.assertIsNone(result["events"][0]["inbound_kind"])
        self.assertEqual(requests[1]["wa_turn_id"], "waturn_current")
        self.assertNotIn("turn_id", requests[1])
        self.assertNotIn("user_message", requests[1])

    def test_context_tool_without_current_turn_is_controlled_unavailable(self) -> None:
        with patch.object(self.tools_module.urllib.request, "urlopen") as opener:
            result = json.loads(
                self.tools_module.conversation_context(
                    {}, BRAIN_GATEWAY_TOKEN="gateway-secret"
                )
            )
        self.assertEqual(result["status"], "unavailable")
        opener.assert_not_called()

    def test_pre_tool_forces_turn_key_for_three_assignees(self) -> None:
        # An origin turn exists only after an external message, so the turn
        # must be preceded by its dispatch (spec 10.1.1).
        self.dispatch(self.inbound_event("3EB0AAA"))
        with patch.object(
            self.tools_module.urllib.request,
            "urlopen",
            side_effect=lambda request, timeout: self.brain_response(request.full_url),
        ):
            self.ctx.hooks["pre_llm_call"](turn_id="opaque-turn", user_message="body")
        for assignee in ("porteiro", "cadastro", "reno"):
            for supplied in (None, "", "phone:553499", "wrong"):
                with self.subTest(assignee=assignee, supplied=supplied):
                    directive = self.ctx.hooks["pre_tool_call"](
                        tool_name="kanban_create",
                        args={"assignee": assignee, "idempotency_key": supplied},
                        turn_id="opaque-turn",
                    )
                    self.assertEqual(directive["action"], "modify")
                    self.assertEqual(
                        directive["args"]["idempotency_key"],
                        f"whatsapp:waturn_current:{assignee}",
                    )

    def test_pre_tool_leaves_unrelated_scope_untouched(self) -> None:
        with patch.object(
            self.tools_module.urllib.request,
            "urlopen",
            side_effect=lambda request, timeout: self.brain_response(request.full_url),
        ):
            self.ctx.hooks["pre_llm_call"](turn_id="opaque-turn", user_message="body")
        cases = (
            ("other_tool", {"assignee": "porteiro"}, "opaque-turn"),
            ("kanban_create", {"assignee": "other"}, "opaque-turn"),
            ("kanban_create", {"assignee": "porteiro"}, "other-turn"),
        )
        for tool_name, args, turn_id in cases:
            with self.subTest(tool_name=tool_name, args=args, turn_id=turn_id):
                self.assertIsNone(
                    self.ctx.hooks["pre_tool_call"](
                        tool_name=tool_name, args=args, turn_id=turn_id
                    )
                )

        values = self.valid_context()
        values["HERMES_SESSION_PROFILE"] = "cadastro"
        self.bind_context(values)
        self.assertIsNone(
            self.ctx.hooks["pre_tool_call"](
                tool_name="kanban_create",
                args={"assignee": "porteiro"},
                turn_id="opaque-turn",
            )
        )

    def test_hook_worker_thread_state_is_available_to_current_turn_tool(self) -> None:
        self.dispatch(self.inbound_event("3EB0AAA"))
        copied = copy_context()

        def invoke_hook() -> None:
            copied.run(
                self.ctx.hooks["pre_llm_call"],
                turn_id="threaded-turn",
                user_message="body",
            )

        with patch.object(
            self.tools_module.urllib.request,
            "urlopen",
            side_effect=lambda request, timeout: self.brain_response(request.full_url),
        ):
            thread = Thread(target=invoke_hook)
            thread.start()
            thread.join()
            result = json.loads(
                self.tools_module.conversation_context(
                    {}, BRAIN_GATEWAY_TOKEN="gateway-secret"
                )
            )
            directive = self.ctx.hooks["pre_tool_call"](
                tool_name="kanban_create",
                args={"assignee": "porteiro"},
                turn_id="threaded-turn",
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            directive["args"]["idempotency_key"],
            "whatsapp:waturn_current:porteiro",
        )


if __name__ == "__main__":
    unittest.main()
