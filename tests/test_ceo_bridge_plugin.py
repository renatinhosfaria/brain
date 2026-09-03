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
        """A four-key event remains valid while Brain rolls forward."""
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

    def test_passes_through_exact_raw_attribution_with_tagged_bytes(self) -> None:
        raw = {
            "sourceId": "source-id",
            "ctwaClid": "ctwa-clid",
            "thumbnail": {
                "$type": "bytes",
                "encoding": "base64",
                "data": "AAEC/w==",
            },
            "unknownNested": [{"untouched": "fixture-value"}],
        }
        payload = self.ok_payload(
            events=[
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ctwa_candidate",
                    "source_app": "instagram",
                    "inbound_kind": None,
                    "external_ad_reply": raw,
                }
            ]
        )

        result = self.call_tool(payload)

        self.assertEqual(result, payload)

    def test_accepts_expanded_event_with_null_raw_attribution(self) -> None:
        payload = self.ok_payload(
            events=[
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ordinary_inbound",
                    "source_app": None,
                    "inbound_kind": None,
                    "external_ad_reply": None,
                }
            ]
        )

        self.assertEqual(self.call_tool(payload), payload)

    @staticmethod
    def confirmed_meta() -> dict[str, str]:
        return {
            "status": "confirmed",
            "ad_id": "101",
            "ad_name": "Spring Sale",
            "campaign_id": "202",
            "campaign_name": "Spring Campaign",
        }

    def test_accepts_confirmed_meta_attribution_on_ctwa_candidate(self) -> None:
        payload = self.ok_payload(
            events=[{
                "event_id": "waevt_safe",
                "transport_kind": "ctwa_candidate",
                "source_app": "instagram",
                "inbound_kind": None,
                "meta_attribution": self.confirmed_meta(),
            }]
        )

        self.assertEqual(self.call_tool(payload), payload)

    def test_accepts_bounded_pending_and_unavailable_meta_attribution(self) -> None:
        for status in ("pending", "unavailable"):
            for meta in ({"status": status}, {"status": status, "reason": "meta_pending"}):
                with self.subTest(status=status, meta=meta):
                    payload = self.ok_payload(
                        events=[{
                            "event_id": "waevt_safe",
                            "transport_kind": "ctwa_candidate",
                            "source_app": "instagram",
                            "inbound_kind": None,
                            "meta_attribution": meta,
                        }]
                    )
                    self.assertEqual(self.call_tool(payload), payload)

    def test_rejects_invalid_meta_attribution_fixtures(self) -> None:
        base = {
            "event_id": "waevt_safe",
            "transport_kind": "ctwa_candidate",
            "source_app": "instagram",
            "inbound_kind": None,
        }
        fixtures = {
            "ordinary event": ({**base, "transport_kind": "ordinary_inbound", "meta_attribution": self.confirmed_meta()}, "Spring Sale"),
            "unknown key": ({**base, "meta_attribution": {**self.confirmed_meta(), "ad_status": "ACTIVE"}}, "ACTIVE"),
            "inactive status": ({**base, "meta_attribution": {**self.confirmed_meta(), "status": "inactive"}}, "inactive"),
            "malformed id": ({**base, "meta_attribution": {**self.confirmed_meta(), "ad_id": "101.0"}}, "101.0"),
            "malformed campaign id": ({**base, "meta_attribution": {**self.confirmed_meta(), "campaign_id": "act_202"}}, "act_202"),
            "missing status": ({**base, "meta_attribution": {"ad_id": "101"}}, "101"),
            "malformed status": ({**base, "meta_attribution": {**self.confirmed_meta(), "status": None}}, "Spring Sale"),
            "missing confirmed field": ({**base, "meta_attribution": {key: value for key, value in self.confirmed_meta().items() if key != "campaign_name"}}, "Spring Sale"),
            "unconfirmed names": ({**base, "meta_attribution": {"status": "pending", "reason": "pending", "ad_name": "Spring Sale"}}, "Spring Sale"),
            "token-shaped field": ({**base, "meta_attribution": {**self.confirmed_meta(), "access_token": "secret-token"}}, "secret-token"),
            "remote-shaped value": ({**base, "meta_attribution": {**self.confirmed_meta(), "ad_id": {"id": "101"}}}, "101"),
            "unsafe name": ({**base, "meta_attribution": {**self.confirmed_meta(), "ad_name": "Spring\nSale"}}, "Spring"),
            "oversized name": ({**base, "meta_attribution": {**self.confirmed_meta(), "ad_name": "n" * 161}}, "n" * 161),
            "oversized reason": ({**base, "meta_attribution": {"status": "pending", "reason": "r" * 81}}, "r" * 81),
            "unsafe reason": ({**base, "meta_attribution": {"status": "pending", "reason": "bad\tvalue"}}, "bad"),
        }
        for label, (event, secret) in fixtures.items():
            with self.subTest(label):
                result = self.call_tool(self.ok_payload(events=[event]))
                self.assertEqual(result, {"status": "unavailable", "reason": "context_unavailable"})
                self.assertNotIn(secret, json.dumps(result))

    def test_rejects_raw_attribution_on_an_ordinary_event(self) -> None:
        payload = self.ok_payload(
            events=[
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ordinary_inbound",
                    "source_app": None,
                    "inbound_kind": None,
                    "external_ad_reply": {"fixture": "ordinary-secret"},
                }
            ]
        )

        result = self.call_tool(payload)

        self.assertEqual(
            result, {"status": "unavailable", "reason": "context_unavailable"}
        )
        self.assertNotIn("ordinary-secret", json.dumps(result))

    def test_rejects_invalid_raw_attribution_tag_without_echoing_it(self) -> None:
        payload = self.ok_payload(
            events=[
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ctwa_candidate",
                    "source_app": "instagram",
                    "inbound_kind": None,
                    "external_ad_reply": {
                        "thumbnail": {
                            "$type": "bytes",
                            "encoding": "base64",
                            "data": "fixture-secret",
                        }
                    },
                }
            ]
        )

        result = self.call_tool(payload)

        self.assertEqual(
            result, {"status": "unavailable", "reason": "context_unavailable"}
        )
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_rejects_integer_valued_unsafe_raw_floats(self) -> None:
        for value in (9007199254740992.0, 1e20):
            payload = self.ok_payload(
                events=[
                    {
                        "event_id": "waevt_safe",
                        "transport_kind": "ctwa_candidate",
                        "source_app": "instagram",
                        "inbound_kind": None,
                        "external_ad_reply": {"futureNumber": value},
                    }
                ]
            )

            with self.subTest(value=value):
                self.assertEqual(
                    self.call_tool(payload),
                    {
                        "status": "unavailable",
                        "reason": "context_unavailable",
                    },
                )

    def test_rejects_raw_attribution_past_depth_and_node_limits(self) -> None:
        too_deep: object = {"fixture": "depth-secret"}
        for _ in range(33):
            too_deep = {"nested": too_deep}
        too_many_nodes = {"nodes": [None] * 10_001}
        for label, raw, secret in (
            ("depth", too_deep, "depth-secret"),
            ("nodes", too_many_nodes, "nodes"),
        ):
            with self.subTest(label=label), patch.object(
                self.tools_module, "_MAX_RESPONSE_BYTES", 1024 * 1024
            ):
                payload = self.ok_payload(
                    events=[
                        {
                            "event_id": "waevt_safe",
                            "transport_kind": "ctwa_candidate",
                            "source_app": "instagram",
                            "inbound_kind": None,
                            "external_ad_reply": raw,
                        }
                    ]
                )

                result = self.call_tool(payload)

                self.assertEqual(
                    result,
                    {"status": "unavailable", "reason": "context_unavailable"},
                )
                self.assertNotIn(secret, json.dumps(result))

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
        huge = self.ok_payload(
            events=[
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ctwa_candidate",
                    "source_app": "instagram",
                    "inbound_kind": None,
                    "external_ad_reply": {"fixture-secret": "x" * 500},
                }
            ]
        )

        with patch.dict(
            os.environ, {"BRAIN_CONTEXT_RESPONSE_MAX_BYTES": "256"}
        ):
            result = self.call_tool(huge)

        self.assertEqual(
            result, {"status": "unavailable", "reason": "context_unavailable"}
        )
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_accepts_response_larger_than_the_legacy_ceiling(self) -> None:
        payload = self.ok_payload(
            events=[
                {
                    "event_id": "waevt_safe",
                    "transport_kind": "ctwa_candidate",
                    "source_app": "instagram",
                    "inbound_kind": None,
                    "external_ad_reply": {"unknown": "x" * 20_000},
                }
            ]
        )

        self.assertEqual(self.call_tool(payload), payload)

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
