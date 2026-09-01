"""Durable per-contact human handover policy for the CEO WhatsApp gateway."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from gateway.whatsapp_identity import (
    canonical_whatsapp_identifier,
    expand_whatsapp_aliases,
)

_OWNER_PREFIX = "[owner reply] "
_HUMAN_PREFIX = "[Atendimento humano] "
_OUTBOUND_ID_TTL_SECONDS = 24 * 60 * 60
_RESUME_AUTHORIZED: ContextVar[str | None] = ContextVar(
    "fama_handover_resume_authorized",
    default=None,
)
logger = logging.getLogger(__name__)


def _phone_aliases(value: str) -> set[str]:
    digits = re.sub(r"\D+", "", str(value or ""))
    if not digits:
        return set()
    aliases = {digits}
    if digits.startswith("55") and len(digits) == 13 and digits[4] == "9":
        aliases.add(digits[:4] + digits[5:])
    elif digits.startswith("55") and len(digits) == 12:
        aliases.add(digits[:4] + "9" + digits[4:])
    return aliases


def _identity_aliases(value: str) -> set[str]:
    aliases: set[str] = set()
    for candidate in expand_whatsapp_aliases(value) or {value}:
        aliases.update(_phone_aliases(candidate))
    return aliases


class HandoverStore:
    """Small durable index of WhatsApp contacts currently in human care."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paused_contacts (
                    contact_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    paused_at REAL NOT NULL,
                    owner_message_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_outbound_messages (
                    message_id TEXT PRIMARY KEY,
                    contact_id TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def from_env(cls) -> HandoverStore:
        configured = os.getenv("FAMA_WHATSAPP_HANDOVER_DB", "").strip()
        if configured:
            return cls(Path(configured))
        home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
        return cls(
            home / "plugin-data" / "fama-whatsapp-human-handover" / "handover.db"
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=2.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def pause(
        self,
        contact_id: str,
        *,
        session_key: str,
        owner_message_id: str | None,
    ) -> None:
        contact_aliases = _identity_aliases(contact_id)
        with self._connect() as conn:
            rows = conn.execute("SELECT contact_id FROM paused_contacts").fetchall()
            for row in rows:
                stored_id = str(row[0])
                if contact_aliases.intersection(_identity_aliases(stored_id)):
                    conn.execute(
                        "DELETE FROM paused_contacts WHERE contact_id = ?",
                        (stored_id,),
                    )
            conn.execute(
                """
                INSERT INTO paused_contacts (
                    contact_id, session_key, paused_at, owner_message_id
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_id) DO UPDATE SET
                    session_key=excluded.session_key,
                    paused_at=excluded.paused_at,
                    owner_message_id=excluded.owner_message_id
                """,
                (contact_id, session_key, time.time(), owner_message_id),
            )

    def is_paused(self, contact_id: str) -> bool:
        requested_aliases = _identity_aliases(contact_id)
        if not requested_aliases:
            return False
        with self._connect() as conn:
            rows = conn.execute("SELECT contact_id FROM paused_contacts").fetchall()
        return any(
            requested_aliases.intersection(_identity_aliases(str(row[0])))
            for row in rows
        )

    def resume(self, requested_phone: str) -> str | None:
        requested_aliases = _phone_aliases(requested_phone)
        if not requested_aliases:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT contact_id FROM paused_contacts ORDER BY paused_at"
            ).fetchall()
            matches = [
                str(row[0])
                for row in rows
                if requested_aliases.intersection(_identity_aliases(str(row[0])))
            ]
            if len(matches) != 1:
                return None
            conn.execute(
                "DELETE FROM paused_contacts WHERE contact_id = ?",
                (matches[0],),
            )
        return matches[0]

    def remember_agent_outbound(
        self,
        contact_id: str,
        message_ids: set[str],
    ) -> None:
        safe_ids = {
            str(message_id).strip()
            for message_id in message_ids
            if str(message_id).strip() and len(str(message_id).strip()) <= 512
        }
        if not safe_ids:
            return
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM agent_outbound_messages WHERE recorded_at < ?",
                (now - _OUTBOUND_ID_TTL_SECONDS,),
            )
            conn.executemany(
                """
                INSERT INTO agent_outbound_messages (
                    message_id, contact_id, recorded_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    contact_id=excluded.contact_id,
                    recorded_at=excluded.recorded_at
                """,
                ((message_id, contact_id, now) for message_id in safe_ids),
            )

    def is_agent_outbound(self, contact_id: str, message_id: str | None) -> bool:
        candidate = str(message_id or "").strip()
        if not candidate:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT contact_id, recorded_at
                FROM agent_outbound_messages
                WHERE message_id = ?
                """,
                (candidate,),
            ).fetchone()
        if row is None or float(row[1]) < time.time() - _OUTBOUND_ID_TTL_SECONDS:
            return False
        return bool(
            _identity_aliases(contact_id).intersection(_identity_aliases(str(row[0])))
        )


def _platform_value(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def _contact_id(event: Any) -> str:
    chat_id = str(getattr(getattr(event, "source", None), "chat_id", "") or "")
    return canonical_whatsapp_identifier(chat_id)


def _session_entry(session_store: Any, event: Any) -> Any:
    return session_store.get_or_create_session(
        event.source,
        touch_activity=False,
    )


def _append_observed(
    session_store: Any,
    session_id: str,
    *,
    role: str,
    content: str,
    message_id: str | None,
) -> None:
    if message_id and session_store.has_platform_message_id(session_id, message_id):
        return
    message = {
        "role": role,
        "content": content,
        "message_id": message_id,
        "observed": True,
    }
    if message_id is None:
        message.pop("message_id")
    session_store.append_to_transcript(session_id, message)


def _message_body(event: Any, *, from_owner: bool) -> str:
    body = str(getattr(event, "text", "") or "")
    if from_owner:
        body = body.removeprefix(_OWNER_PREFIX)
    media_urls = list(getattr(event, "media_urls", None) or [])
    media_types = list(getattr(event, "media_types", None) or [])
    attachments = []
    for index, media_url in enumerate(media_urls):
        media_type = media_types[index] if index < len(media_types) else "media"
        attachments.append(f"{media_type}: {media_url}")
    if attachments:
        descriptor = "[Anexos observados durante atendimento humano: "
        descriptor += "; ".join(attachments) + "]"
        body = f"{body}\n{descriptor}" if body else descriptor
    return f"{_HUMAN_PREFIX}{body}" if from_owner else body


def _interrupt_running_turn(gateway: Any, event: Any) -> None:
    try:
        session_key = gateway._session_key_for_source(event.source)
        running = getattr(gateway, "_running_agents", {}).get(session_key)
        if running is None:
            return
        invalidate = getattr(gateway, "_invalidate_session_run_generation", None)
        if callable(invalidate):
            invalidate(session_key, reason="human_handover")
        interrupt = getattr(running, "interrupt", None)
        if callable(interrupt):
            interrupt("[control interrupt: human handover]")
        hard_interrupt = getattr(gateway, "_interrupt_and_clear_session", None)
        if callable(hard_interrupt):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(
                    hard_interrupt(
                        session_key,
                        event.source,
                        interrupt_reason="human_handover",
                        invalidation_reason="human_handover",
                    )
                )
                return
    except Exception:  # noqa: BLE001 - interruption must never release dispatch
        return


def _resume_args(text: str) -> str | None:
    parts = str(text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    command = parts[0].lower().split("@", 1)[0]
    if command != "/retomar":
        return None
    return parts[1].strip() if len(parts) == 2 else ""


def _is_authorized_resume_source(event: Any) -> bool:
    source = getattr(event, "source", None)
    return (
        _platform_value(event) == "telegram"
        and str(getattr(source, "chat_id", "") or "")
        == os.getenv("FAMA_HANDOVER_TELEGRAM_CHAT_ID", "").strip()
        and str(getattr(source, "thread_id", "") or "")
        == os.getenv("FAMA_HANDOVER_TELEGRAM_THREAD_ID", "").strip()
        and str(getattr(source, "user_id", "") or "")
        == os.getenv("FAMA_HANDOVER_TELEGRAM_USER_ID", "").strip()
        and all(
            os.getenv(name, "").strip()
            for name in (
                "FAMA_HANDOVER_TELEGRAM_CHAT_ID",
                "FAMA_HANDOVER_TELEGRAM_THREAD_ID",
                "FAMA_HANDOVER_TELEGRAM_USER_ID",
            )
        )
    )


def resume_command(raw_args: str) -> str:
    authorized_args = _RESUME_AUTHORIZED.get()
    _RESUME_AUTHORIZED.set(None)
    digits = re.sub(r"\D+", "", str(raw_args or ""))
    if authorized_args is None or authorized_args != digits:
        return "⛔ Comando disponível somente no Telegram administrativo do CEO."
    if not 10 <= len(digits) <= 15:
        return "Uso: /retomar <telefone>"
    try:
        resumed = HandoverStore.from_env().resume(digits)
    except Exception:
        logger.exception("WhatsApp handover resume operation failed")
        return "⛔ Não foi possível alterar a pausa. Tente novamente."
    if resumed is None:
        return f"ℹ️ Nenhum atendimento humano pausado para {digits}."
    return f"▶️ Atendimento automático retomado para {digits}."


def _handle_whatsapp_handover(
    *,
    event: Any,
    gateway: Any,
    session_store: Any,
    interrupt_owner: bool,
):
    if _platform_value(event) != "whatsapp":
        return None
    source = getattr(event, "source", None)
    if str(getattr(source, "chat_type", "") or "").lower() != "dm":
        return None
    contact_id = _contact_id(event)
    from_owner = bool(
        (getattr(event, "metadata", None) or {}).get("whatsapp_from_owner")
    )
    try:
        store = HandoverStore.from_env()
        paused = store.is_paused(contact_id)
        if bool(getattr(event, "internal", False)):
            if paused:
                return {"action": "skip", "reason": "human_handover_internal"}
            return None
        if not from_owner and not paused:
            return None

        if from_owner and store.is_agent_outbound(
            contact_id,
            getattr(event, "message_id", None),
        ):
            return {"action": "skip", "reason": "human_handover_agent_echo"}
        entry = _session_entry(session_store, event)
        if from_owner:
            store.pause(
                contact_id,
                session_key=entry.session_key,
                owner_message_id=getattr(event, "message_id", None),
            )

        role = "user"
        if from_owner:
            role = "assistant"
        _append_observed(
            session_store,
            entry.session_id,
            role=role,
            content=_message_body(event, from_owner=from_owner),
            message_id=getattr(event, "message_id", None),
        )
        if from_owner and interrupt_owner:
            _interrupt_running_turn(gateway, event)
        return {"action": "skip", "reason": "human_handover"}
    except Exception:
        logger.exception("WhatsApp handover state or transcript operation failed")
        if from_owner and interrupt_owner:
            _interrupt_running_turn(gateway, event)
        return {"action": "skip", "reason": "human_handover_state_unavailable"}


def pre_gateway_dispatch(*, event: Any, gateway: Any, session_store: Any, **_: Any):
    """Pause and silently ingest a WhatsApp owner-authored DM."""
    _RESUME_AUTHORIZED.set(None)
    resume_args = _resume_args(getattr(event, "text", ""))
    if resume_args is not None:
        if not _is_authorized_resume_source(event):
            return {"action": "skip", "reason": "unauthorized_handover_command"}
        _RESUME_AUTHORIZED.set(re.sub(r"\D+", "", resume_args))
        return None
    return _handle_whatsapp_handover(
        event=event,
        gateway=gateway,
        session_store=session_store,
        interrupt_owner=True,
    )


def _wire_whatsapp_adapter(_native_handler: Any, adapter: Any) -> None:
    """Intercept active-session events before Hermes can queue them."""
    if bool(getattr(adapter, "_fama_handover_wrapped", False)):
        return
    original_busy = getattr(adapter, "_busy_session_handler", None)
    if not callable(original_busy):
        logger.error("WhatsApp handover could not find the Hermes busy handler")
        return
    gateway = getattr(original_busy, "__self__", None)
    session_store = getattr(gateway, "session_store", None)
    hard_interrupt = getattr(gateway, "_interrupt_and_clear_session", None)
    if session_store is None or not callable(hard_interrupt):
        logger.error("WhatsApp handover could not bind the Hermes gateway runtime")
        return

    async def handover_busy_handler(event: Any, session_key: str) -> bool:
        decision = _handle_whatsapp_handover(
            event=event,
            gateway=gateway,
            session_store=session_store,
            interrupt_owner=False,
        )
        if not decision or decision.get("action") != "skip":
            return await original_busy(event, session_key)
        from_owner = bool(
            (getattr(event, "metadata", None) or {}).get("whatsapp_from_owner")
        )
        if from_owner and decision.get("reason") != "human_handover_agent_echo":
            try:
                await hard_interrupt(
                    session_key,
                    event.source,
                    interrupt_reason="human_handover",
                    invalidation_reason="human_handover",
                )
            except Exception:
                logger.exception("WhatsApp handover hard interrupt failed")
                invalidate = getattr(
                    gateway,
                    "_invalidate_session_run_generation",
                    None,
                )
                if callable(invalidate):
                    invalidate(session_key, reason="human_handover_fallback")
        return True

    def outbound_ids(result: Any) -> set[str]:
        if not bool(getattr(result, "success", False)):
            return set()
        ids = {
            str(message_id)
            for message_id in (
                getattr(result, "message_id", None),
                *(getattr(result, "continuation_message_ids", None) or ()),
            )
            if message_id
        }
        raw = getattr(result, "raw_response", None)
        if isinstance(raw, dict):
            for key in ("message_ids", "messageIds"):
                ids.update(
                    str(message_id) for message_id in (raw.get(key) or ()) if message_id
                )
        return ids

    def wrap_outbound(method_name: str) -> None:
        original = getattr(adapter, method_name, None)
        if not callable(original):
            return

        async def tracked_outbound(*args: Any, **kwargs: Any):
            result = await original(*args, **kwargs)
            ids = outbound_ids(result)
            chat_id = args[0] if args else kwargs.get("chat_id")
            if ids and chat_id:
                try:
                    HandoverStore.from_env().remember_agent_outbound(
                        canonical_whatsapp_identifier(str(chat_id)),
                        ids,
                    )
                except Exception:
                    logger.exception("WhatsApp outbound ID tracking failed")
            return result

        setattr(adapter, method_name, tracked_outbound)

    adapter.set_busy_session_handler(handover_busy_handler)
    for method_name in (
        "send",
        "edit_message",
        "_send_media_to_bridge",
        "send_poll",
        "send_location",
    ):
        wrap_outbound(method_name)
    adapter._fama_handover_wrapped = True


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_platform_handler("whatsapp", _wire_whatsapp_adapter)
    ctx.register_command(
        "retomar",
        resume_command,
        description="Retomar o atendimento automático de um contato do WhatsApp.",
        args_hint="<telefone>",
    )
