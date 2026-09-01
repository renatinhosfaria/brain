from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN_DIR = (
    Path(__file__).parents[1]
    / "integrations"
    / "hermes"
    / "fama-whatsapp-human-handover"
)


def load_plugin():
    source = PLUGIN_DIR / "__init__.py"
    if not source.exists():
        raise AssertionError("handover plugin module is not implemented")
    spec = importlib.util.spec_from_file_location("fama_whatsapp_handover", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSessionStore:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []
        self.transcript: list[dict] = []

    def get_or_create_session(self, source, touch_activity=False):
        return SimpleNamespace(
            session_id="session-one",
            session_key="agent:main:whatsapp:dm:553499602714",
        )

    def has_platform_message_id(self, session_id, message_id):
        return any(
            row_session == session_id and row.get("message_id") == message_id
            for row_session, row in self.messages
        )

    def append_to_transcript(self, session_id, message):
        self.messages.append((session_id, dict(message)))

    def load_transcript(self, session_id):
        return list(self.transcript)


class FakeAgent:
    def __init__(self) -> None:
        self.interrupts: list[str] = []

    def interrupt(self, reason):
        self.interrupts.append(reason)


class FakeGateway:
    def __init__(self, agent: FakeAgent) -> None:
        self._running_agents = {
            "agent:main:whatsapp:dm:553499602714": agent,
        }
        self.invalidations: list[tuple[str, str]] = []

    def _session_key_for_source(self, source):
        return "agent:main:whatsapp:dm:553499602714"

    def _invalidate_session_run_generation(self, session_key, *, reason):
        self.invalidations.append((session_key, reason))


class AsyncHardGateway(FakeGateway):
    def __init__(self, agent: FakeAgent) -> None:
        super().__init__(agent)
        self.hard_interrupts: list[tuple[str, object, str, str]] = []

    async def _interrupt_and_clear_session(
        self,
        session_key,
        source,
        *,
        interrupt_reason,
        invalidation_reason,
    ):
        self.hard_interrupts.append(
            (session_key, source, interrupt_reason, invalidation_reason)
        )


class BusyGateway(AsyncHardGateway):
    def __init__(self, agent: FakeAgent, session_store: FakeSessionStore) -> None:
        super().__init__(agent)
        self.session_store = session_store
        self.original_busy_calls: list[tuple[object, str]] = []

    async def original_busy(self, event, session_key):
        self.original_busy_calls.append((event, session_key))
        return False


class BusyAdapter:
    def __init__(self, gateway: BusyGateway) -> None:
        self.gateway = gateway
        self._busy_session_handler = gateway.original_busy

    @property
    def original_busy_calls(self):
        return self.gateway.original_busy_calls

    def set_busy_session_handler(self, handler):
        self._busy_session_handler = handler


def event(
    *,
    text: str,
    message_id: str,
    from_owner: bool,
    media_urls: list[str] | None = None,
    media_types: list[str] | None = None,
):
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        metadata={"whatsapp_from_owner": from_owner},
        media_urls=media_urls or [],
        media_types=media_types or [],
        source=SimpleNamespace(
            platform=SimpleNamespace(value="whatsapp"),
            chat_type="dm",
            chat_id="553499602714@s.whatsapp.net",
            user_id="553499602714@s.whatsapp.net",
            thread_id=None,
        ),
    )


def telegram_event(*, text: str, user_id: str = "8564576789"):
    return SimpleNamespace(
        text=text,
        message_id="telegram-1",
        metadata={},
        source=SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            chat_type="group",
            chat_id="-1004374717222",
            user_id=user_id,
            thread_id="1",
        ),
    )


class WhatsAppHandoverPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "FAMA_WHATSAPP_HANDOVER_DB": str(
                    Path(self.temp_dir.name) / "handover.db"
                ),
                "FAMA_HANDOVER_TELEGRAM_CHAT_ID": "-1004374717222",
                "FAMA_HANDOVER_TELEGRAM_THREAD_ID": "1",
                "FAMA_HANDOVER_TELEGRAM_USER_ID": "8564576789",
                "HERMES_HOME": self.temp_dir.name,
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self._install_whatsapp_identity_stub()

    def _install_whatsapp_identity_stub(self) -> None:
        old_modules = {
            name: sys.modules.get(name)
            for name in ("gateway", "gateway.whatsapp_identity")
        }
        gateway = types.ModuleType("gateway")
        identity = types.ModuleType("gateway.whatsapp_identity")

        def bare(value: str) -> str:
            return re.sub(r"@.*$", "", str(value or "")).split(":", 1)[0]

        def expand_whatsapp_aliases(value: str) -> set[str]:
            candidate = bare(value)
            aliases = {candidate} if candidate else set()
            session_dir = (
                Path(os.environ["HERMES_HOME"]) / "platforms" / "whatsapp" / "session"
            )
            for suffix in ("", "_reverse"):
                path = session_dir / f"lid-mapping-{candidate}{suffix}.json"
                try:
                    mapped = bare(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
                if mapped:
                    aliases.add(mapped)
            return aliases

        def canonical_whatsapp_identifier(value: str) -> str:
            aliases = expand_whatsapp_aliases(value)
            phone = next(
                (
                    alias
                    for alias in aliases
                    if alias.startswith("55") and len(alias) in {12, 13}
                ),
                None,
            )
            return phone or bare(value)

        identity.canonical_whatsapp_identifier = canonical_whatsapp_identifier
        identity.expand_whatsapp_aliases = expand_whatsapp_aliases
        gateway.whatsapp_identity = identity
        sys.modules["gateway"] = gateway
        sys.modules["gateway.whatsapp_identity"] = identity

        def restore() -> None:
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)

    def test_owner_reply_activates_handover_and_is_silently_ingested(self):
        plugin = load_plugin()
        sessions = FakeSessionStore()
        agent = FakeAgent()

        result = plugin.pre_gateway_dispatch(
            event=event(
                text="[owner reply] Vou assumir daqui.",
                message_id="owner-1",
                from_owner=True,
            ),
            gateway=FakeGateway(agent),
            session_store=sessions,
        )

        self.assertEqual(result, {"action": "skip", "reason": "human_handover"})
        self.assertEqual(len(agent.interrupts), 1)
        self.assertEqual(
            sessions.messages,
            [
                (
                    "session-one",
                    {
                        "role": "assistant",
                        "content": "[Atendimento humano] Vou assumir daqui.",
                        "message_id": "owner-1",
                        "observed": True,
                    },
                )
            ],
        )
        self.assertTrue(plugin.HandoverStore.from_env().is_paused("553499602714"))

    def test_owner_reply_hard_interrupts_an_inflight_ceo_turn(self):
        plugin = load_plugin()
        sessions = FakeSessionStore()
        agent = FakeAgent()
        gateway = AsyncHardGateway(agent)
        owner_event = event(
            text="[owner reply] Vou assumir daqui.",
            message_id="owner-hard-1",
            from_owner=True,
        )

        async def scenario():
            result = plugin.pre_gateway_dispatch(
                event=owner_event,
                gateway=gateway,
                session_store=sessions,
            )
            await asyncio.sleep(0)
            return result

        result = asyncio.run(scenario())

        self.assertEqual(result, {"action": "skip", "reason": "human_handover"})
        self.assertEqual(agent.interrupts, ["[control interrupt: human handover]"])
        self.assertEqual(
            gateway.invalidations,
            [
                (
                    "agent:main:whatsapp:dm:553499602714",
                    "human_handover",
                )
            ],
        )
        self.assertEqual(
            gateway.hard_interrupts,
            [
                (
                    "agent:main:whatsapp:dm:553499602714",
                    owner_event.source,
                    "human_handover",
                    "human_handover",
                )
            ],
        )

    def test_owner_reply_bypasses_busy_queue_and_awaits_hard_stop(self):
        plugin = load_plugin()
        sessions = FakeSessionStore()
        agent = FakeAgent()
        gateway = BusyGateway(agent, sessions)
        adapter = BusyAdapter(gateway)
        plugin._wire_whatsapp_adapter(None, adapter)
        owner_event = event(
            text="[owner reply] Vou assumir agora.",
            message_id="owner-busy-1",
            from_owner=True,
        )

        consumed = asyncio.run(
            adapter._busy_session_handler(
                owner_event,
                "agent:main:whatsapp:dm:553499602714",
            )
        )

        self.assertTrue(consumed)
        self.assertEqual(adapter.original_busy_calls, [])
        self.assertEqual(len(gateway.hard_interrupts), 1)
        self.assertTrue(plugin.HandoverStore.from_env().is_paused("553499602714"))
        self.assertEqual(len(sessions.messages), 1)

    def test_busy_paused_customer_message_is_consumed_without_queueing(self):
        plugin = load_plugin()
        plugin.HandoverStore.from_env().pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-1",
        )
        sessions = FakeSessionStore()
        gateway = BusyGateway(FakeAgent(), sessions)
        adapter = BusyAdapter(gateway)
        plugin._wire_whatsapp_adapter(None, adapter)

        consumed = asyncio.run(
            adapter._busy_session_handler(
                event(
                    text="Continuando enquanto você atende.",
                    message_id="customer-busy-1",
                    from_owner=False,
                ),
                "agent:main:whatsapp:dm:553499602714",
            )
        )

        self.assertTrue(consumed)
        self.assertEqual(adapter.original_busy_calls, [])
        self.assertEqual(len(sessions.messages), 1)

    def test_paused_customer_message_is_ingested_without_reaching_the_llm(self):
        plugin = load_plugin()
        plugin.HandoverStore.from_env().pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-1",
        )
        sessions = FakeSessionStore()

        result = plugin.pre_gateway_dispatch(
            event=event(
                text="Ainda está disponível?",
                message_id="customer-2",
                from_owner=False,
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertEqual(result, {"action": "skip", "reason": "human_handover"})
        self.assertEqual(
            sessions.messages,
            [
                (
                    "session-one",
                    {
                        "role": "user",
                        "content": "Ainda está disponível?",
                        "message_id": "customer-2",
                        "observed": True,
                    },
                )
            ],
        )

    def test_paused_media_references_are_preserved_in_the_transcript(self):
        plugin = load_plugin()
        plugin.HandoverStore.from_env().pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-1",
        )
        sessions = FakeSessionStore()

        result = plugin.pre_gateway_dispatch(
            event=event(
                text="",
                message_id="customer-audio-1",
                from_owner=False,
                media_urls=["/tmp/hermes/audio-customer-audio-1.ogg"],
                media_types=["audio/ogg"],
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertEqual(result, {"action": "skip", "reason": "human_handover"})
        content = sessions.messages[0][1]["content"]
        self.assertIn("audio/ogg", content)
        self.assertIn("/tmp/hermes/audio-customer-audio-1.ogg", content)

    def test_recent_agent_echo_after_bridge_restart_does_not_pause_contact(self):
        plugin = load_plugin()
        sessions = FakeSessionStore()
        sessions.transcript = [
            {
                "role": "assistant",
                "content": "Resposta que acabou de sair.",
                "timestamp": time.time(),
                "observed": False,
            }
        ]

        result = plugin.pre_gateway_dispatch(
            event=event(
                text="[owner reply] Resposta que acabou de sair.",
                message_id="echo-after-restart",
                from_owner=True,
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "human_handover_agent_echo"},
        )
        self.assertFalse(plugin.HandoverStore.from_env().is_paused("553499602714"))
        self.assertEqual(sessions.messages, [])

    def test_recent_chunked_agent_echo_after_restart_does_not_pause_contact(self):
        plugin = load_plugin()
        sessions = FakeSessionStore()
        sessions.transcript = [
            {
                "role": "assistant",
                "content": (
                    "Primeira parte da resposta. "
                    "Esta é a segunda parte entregue separadamente."
                ),
                "timestamp": time.time(),
                "observed": False,
            }
        ]

        result = plugin.pre_gateway_dispatch(
            event=event(
                text=("[owner reply] Esta é a segunda parte entregue separadamente."),
                message_id="chunk-echo-after-restart",
                from_owner=True,
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "human_handover_agent_echo"},
        )
        self.assertFalse(plugin.HandoverStore.from_env().is_paused("553499602714"))

    def test_pause_survives_lid_to_phone_identity_transition(self):
        session_dir = Path(self.temp_dir.name) / "platforms" / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        lid = "164785463279660"
        (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
            '"553499602714"',
            encoding="utf-8",
        )
        (session_dir / "lid-mapping-553499602714.json").write_text(
            f'"{lid}"',
            encoding="utf-8",
        )
        plugin = load_plugin()
        plugin.HandoverStore.from_env().pause(
            lid,
            session_key=f"agent:main:whatsapp:dm:{lid}",
            owner_message_id="owner-lid-1",
        )
        sessions = FakeSessionStore()

        result = plugin.pre_gateway_dispatch(
            event=event(
                text="Mensagem depois da troca de identidade.",
                message_id="customer-after-lid",
                from_owner=False,
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertEqual(result, {"action": "skip", "reason": "human_handover"})
        self.assertEqual(len(sessions.messages), 1)

    def test_handover_state_failure_fails_closed_without_invoking_the_llm(self):
        plugin = load_plugin()
        with (
            self.assertLogs(plugin.logger, level="ERROR"),
            patch.object(
                plugin.HandoverStore,
                "from_env",
                side_effect=OSError("handover database unavailable"),
            ),
        ):
            result = plugin.pre_gateway_dispatch(
                event=event(
                    text="O banco da pausa está indisponível.",
                    message_id="customer-db-failure",
                    from_owner=False,
                ),
                gateway=FakeGateway(FakeAgent()),
                session_store=FakeSessionStore(),
            )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "human_handover_state_unavailable"},
        )

    def test_whatsapp_redelivery_does_not_duplicate_the_silent_transcript(self):
        plugin = load_plugin()
        plugin.HandoverStore.from_env().pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-1",
        )
        sessions = FakeSessionStore()
        customer_event = event(
            text="Mensagem entregue novamente.",
            message_id="customer-redelivery-1",
            from_owner=False,
        )

        first = plugin.pre_gateway_dispatch(
            event=customer_event,
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )
        second = plugin.pre_gateway_dispatch(
            event=customer_event,
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertEqual(first, {"action": "skip", "reason": "human_handover"})
        self.assertEqual(second, first)
        self.assertEqual(len(sessions.messages), 1)

    def test_unpaused_customer_message_keeps_the_normal_ceo_flow(self):
        plugin = load_plugin()
        sessions = FakeSessionStore()

        result = plugin.pre_gateway_dispatch(
            event=event(
                text="Quero saber mais.",
                message_id="customer-1",
                from_owner=False,
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=sessions,
        )

        self.assertIsNone(result)
        self.assertEqual(sessions.messages, [])

    def test_admin_telegram_command_resumes_legacy_brazilian_phone_alias(self):
        plugin = load_plugin()
        store = plugin.HandoverStore.from_env()
        store.pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-1",
        )

        gate = plugin.pre_gateway_dispatch(
            event=telegram_event(text="/retomar 5534999602714"),
            gateway=FakeGateway(FakeAgent()),
            session_store=FakeSessionStore(),
        )
        resume = getattr(plugin, "resume_command", None)
        self.assertTrue(callable(resume), "resume command is not implemented")
        response = resume("5534999602714")

        self.assertIsNone(gate)
        self.assertEqual(
            response,
            "▶️ Atendimento automático retomado para 5534999602714.",
        )
        self.assertFalse(store.is_paused("553499602714"))

    def test_admin_resume_matches_a_paused_lid_through_hermes_identity_mapping(self):
        session_dir = Path(self.temp_dir.name) / "platforms" / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        lid = "164785463279660"
        (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
            '"553499602714"',
            encoding="utf-8",
        )
        (session_dir / "lid-mapping-553499602714.json").write_text(
            f'"{lid}"',
            encoding="utf-8",
        )
        plugin = load_plugin()
        store = plugin.HandoverStore.from_env()
        store.pause(
            lid,
            session_key=f"agent:main:whatsapp:dm:{lid}",
            owner_message_id="owner-lid-1",
        )

        gate = plugin.pre_gateway_dispatch(
            event=telegram_event(text="/retomar 5534999602714"),
            gateway=FakeGateway(FakeAgent()),
            session_store=FakeSessionStore(),
        )
        response = plugin.resume_command("5534999602714")

        self.assertIsNone(gate)
        self.assertEqual(
            response,
            "▶️ Atendimento automático retomado para 5534999602714.",
        )
        self.assertFalse(store.is_paused(lid))

    def test_pause_collapses_phone_and_lid_alias_rows_before_resume(self):
        plugin = load_plugin()
        store = plugin.HandoverStore.from_env()
        lid = "164785463279660"
        store.pause(
            lid,
            session_key=f"agent:main:whatsapp:dm:{lid}",
            owner_message_id="owner-lid-before-mapping",
        )
        session_dir = Path(self.temp_dir.name) / "platforms" / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
            '"553499602714"',
            encoding="utf-8",
        )
        (session_dir / "lid-mapping-553499602714.json").write_text(
            f'"{lid}"',
            encoding="utf-8",
        )

        store.pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-phone-after-mapping",
        )
        resumed = store.resume("5534999602714")

        self.assertEqual(resumed, "553499602714")
        self.assertFalse(store.is_paused(lid))

    def test_resume_command_from_non_admin_is_denied_and_pause_remains(self):
        plugin = load_plugin()
        store = plugin.HandoverStore.from_env()
        store.pause(
            "553499602714",
            session_key="agent:main:whatsapp:dm:553499602714",
            owner_message_id="owner-1",
        )

        gate = plugin.pre_gateway_dispatch(
            event=telegram_event(
                text="/retomar 5534999602714",
                user_id="9999999999",
            ),
            gateway=FakeGateway(FakeAgent()),
            session_store=FakeSessionStore(),
        )
        response = plugin.resume_command("5534999602714")

        self.assertEqual(
            gate,
            {"action": "skip", "reason": "unauthorized_handover_command"},
        )
        self.assertEqual(
            response,
            "⛔ Comando disponível somente no Telegram administrativo do CEO.",
        )
        self.assertTrue(store.is_paused("553499602714"))

    def test_resume_authorization_is_bound_to_the_exact_command_arguments(self):
        plugin = load_plugin()
        store = plugin.HandoverStore.from_env()
        for phone in ("553499602714", "553199887766"):
            store.pause(
                phone,
                session_key=f"agent:main:whatsapp:dm:{phone}",
                owner_message_id=f"owner-{phone}",
            )

        gate = plugin.pre_gateway_dispatch(
            event=telegram_event(text="/retomar 5534999602714"),
            gateway=FakeGateway(FakeAgent()),
            session_store=FakeSessionStore(),
        )
        response = plugin.resume_command("553199887766")

        self.assertIsNone(gate)
        self.assertEqual(
            response,
            "⛔ Comando disponível somente no Telegram administrativo do CEO.",
        )
        self.assertTrue(store.is_paused("553499602714"))
        self.assertTrue(store.is_paused("553199887766"))

    def test_resume_state_failure_returns_a_safe_error(self):
        plugin = load_plugin()
        gate = plugin.pre_gateway_dispatch(
            event=telegram_event(text="/retomar 5534999602714"),
            gateway=FakeGateway(FakeAgent()),
            session_store=FakeSessionStore(),
        )

        with (
            self.assertLogs(plugin.logger, level="ERROR"),
            patch.object(
                plugin.HandoverStore,
                "from_env",
                side_effect=OSError("handover database unavailable"),
            ),
        ):
            response = plugin.resume_command("5534999602714")

        self.assertIsNone(gate)
        self.assertEqual(
            response,
            "⛔ Não foi possível alterar a pausa. Tente novamente.",
        )

    def test_registers_handover_hook_platform_guard_and_resume_command(self):
        plugin = load_plugin()
        hooks: list[tuple[str, object]] = []
        commands: list[tuple[str, object, str, str]] = []
        platform_handlers: list[tuple[str, object]] = []

        class Context:
            def register_hook(self, name, callback):
                hooks.append((name, callback))

            def register_platform_handler(self, name, callback):
                platform_handlers.append((name, callback))

            def register_command(
                self,
                name,
                handler,
                description="",
                args_hint="",
            ):
                commands.append((name, handler, description, args_hint))

        register = getattr(plugin, "register", None)
        self.assertTrue(callable(register), "plugin registration is not implemented")
        register(Context())

        self.assertEqual(hooks, [("pre_gateway_dispatch", plugin.pre_gateway_dispatch)])
        self.assertEqual(
            platform_handlers,
            [("whatsapp", plugin._wire_whatsapp_adapter)],
        )
        self.assertEqual(
            commands,
            [
                (
                    "retomar",
                    plugin.resume_command,
                    "Retomar o atendimento automático de um contato do WhatsApp.",
                    "<telefone>",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
