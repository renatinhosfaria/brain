"""Brain application service: authorization, projection, pagination and search."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .authorization import (
    Authorizer,
    Capability,
    GatewaySessionContext,
)
from .config import BrainSettings
from .db import ReadOnlyDatabase, SchemaGuard
from .errors import BrainError, DatabaseUnavailable, InvalidRequest
from .projection import ProjectedMessage, project_rows
from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds
from .transport_service import TransportService
from .whatsapp_identity import PhoneResolution, resolve_phone

logger = logging.getLogger("brain.audit")

# The CEO asks who it is speaking to now, so the answer is bounded to the
# conversation at hand. Six hours covers a click and the exchange it starts,
# including a lead who returns the same afternoon, without turning the reply
# into an attribution history of the contact.
CONTEXT_WINDOW_SECONDS = 6 * 60 * 60
CONTEXT_MAX_EVENTS = 8
CURSOR_VERSION = 1
TRUNCATION_MARKER = "\n[… truncated …]"
FORBIDDEN_ARGUMENTS = frozenset(
    {
        "phone",
        "chat_id",
        "session_id",
        "session_key",
        "task_id",
        "run_id",
        "profile",
        "database_path",
        "board",
        "conversation_id",
        "mapping_dir",
        "whatsapp_session_dir",
    }
)


@dataclass(frozen=True)
class Health:
    status: str
    hermes_state_db: str
    hermes_kanban_db: str
    runtime_db: str
    whatsapp_identity: str
    gateway_bridge: str
    schema: str
    hermes_compatibility: str

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "hermes_state_db": self.hermes_state_db,
            "hermes_kanban_db": self.hermes_kanban_db,
            "runtime_db": self.runtime_db,
            "whatsapp_identity": self.whatsapp_identity,
            "gateway_bridge": self.gateway_bridge,
            "schema": self.schema,
            "hermes_compatibility": self.hermes_compatibility,
        }


class BrainService:
    def __init__(self, settings: BrainSettings) -> None:
        self.settings = settings
        self.state = ReadOnlyDatabase(
            settings.state_db,
            retries=settings.busy_retries,
            timeout_seconds=settings.busy_timeout_seconds,
        )
        self.kanban = ReadOnlyDatabase(
            settings.kanban_db,
            retries=settings.busy_retries,
            timeout_seconds=settings.busy_timeout_seconds,
        )
        self.runtime = RuntimeDatabase(
            settings.runtime_db,
            timeout_seconds=settings.busy_timeout_seconds,
        )
        self.runtime_ids: RuntimeIds | None = None
        if settings.transport_hmac_secret:
            self.runtime_ids = RuntimeIds(settings.transport_hmac_secret)
            self.runtime.initialize()
        self.transport_service = TransportService(
            settings,
            self.runtime,
            self.runtime_ids,
        )
        self.schema = SchemaGuard(self.state, self.kanban)
        self.authorizer = Authorizer(settings, self.state, self.kanban)

    def health(self) -> Health:
        state_ok = self._db_openable(self.state)
        kanban_ok = self._db_openable(self.kanban)
        schema_ok = state_ok and kanban_ok and self.schema.check()
        identity_ok = self._identity_directory_compatible()
        gateway_ok = self._gateway_bridge_configured()
        runtime_ok = self._runtime_compatible()
        compatibility_ok = schema_ok and gateway_ok
        return Health(
            status=(
                "ok"
                if compatibility_ok and identity_ok and runtime_ok
                else "unavailable"
            ),
            hermes_state_db="ok" if state_ok else "unavailable",
            hermes_kanban_db="ok" if kanban_ok else "unavailable",
            runtime_db="ok" if runtime_ok else "unavailable",
            whatsapp_identity="compatible" if identity_ok else "incompatible",
            gateway_bridge="configured" if gateway_ok else "unconfigured",
            schema="compatible" if schema_ok else "incompatible",
            hermes_compatibility=("compatible" if compatibility_ok else "incompatible"),
        )

    def _identity_directory_compatible(self) -> bool:
        path = self.settings.whatsapp_session_dir
        try:
            return (
                not path.is_symlink()
                and path.is_dir()
                and os.access(path, os.R_OK | os.X_OK)
            )
        except OSError:
            return False

    def _gateway_bridge_configured(self) -> bool:
        gateway = self.settings.principals.get("default")
        return bool(
            gateway
            and gateway.mode == "gateway"
            and "conversation_context" in gateway.tools
        )

    def _runtime_compatible(self) -> bool:
        if self.runtime_ids is None or not self.runtime.path.is_file():
            return False
        required = {"transport_events", "contact_ephemera"}
        try:
            return self.runtime.read(
                lambda conn: required.issubset(
                    {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table' AND name IN (?, ?)",
                            tuple(sorted(required)),
                        )
                    }
                )
            )
        except (OSError, sqlite3.Error):
            return False

    @staticmethod
    def _db_openable(db: ReadOnlyDatabase) -> bool:
        try:
            return db.read(lambda _conn: True)
        except (DatabaseUnavailable, sqlite3.Error):
            return False

    def _audit(
        self,
        *,
        identity: Mapping[str, Any],
        tool: str,
        decision: str,
        duration_ms: float,
        message_count: int = 0,
        hit_count: int = 0,
        error: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "event": "brain_conversation_access",
            "timestamp": datetime.now(UTC).isoformat(),
            "profile": identity.get("profile", "unknown"),
            "mode": identity.get("mode", "unknown"),
            "task_id": identity.get("task_id"),
            "run_id": identity.get("run_id"),
            "decision": decision,
            "tool": tool,
            "latency_ms": round(duration_ms, 3),
            "message_count": message_count,
            "hit_count": hit_count,
        }
        if error:
            event["error"] = error
        # json.dumps escapes malicious header values and keeps the event one line.
        logger.info(json.dumps(event, ensure_ascii=True, separators=(",", ":")))

    def call_tool(
        self, tool: str, arguments: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        started = time.perf_counter()
        identity: dict[str, Any] = {
            "profile": "unknown",
            "mode": "unknown",
            "task_id": None,
            "run_id": None,
        }
        try:
            request_identity = self.authorizer.parse_worker_headers(headers)
            identity["profile"] = request_identity.profile
            identity["mode"] = self.settings.principals[request_identity.principal].mode
            if tool not in {
                "conversation_recent",
                "conversation_search",
                "conversation_phone",
            }:
                raise BrainError("AUTH_TASK_INVALID")
            principal = self.settings.principals[request_identity.principal]
            if tool not in principal.tools:
                raise BrainError("AUTH_TOOL_DENIED")
            if not isinstance(arguments, Mapping) or FORBIDDEN_ARGUMENTS.intersection(
                arguments
            ):
                raise BrainError("AUTH_TASK_INVALID")
            if tool == "conversation_phone" and arguments:
                raise BrainError("AUTH_TASK_INVALID")
            capability = self.authorizer.authorize_worker(request_identity, identity)
            identity.update(
                profile=capability.profile,
                task_id=capability.task_id,
                run_id=capability.run_id,
            )
            if tool == "conversation_recent":
                result = self.conversation_recent(capability, arguments)
                count = len(result["messages"])
                self._audit(
                    identity=identity,
                    tool=tool,
                    decision="allow",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    message_count=count,
                )
                return result
            if tool == "conversation_phone":
                result, reason = self._conversation_phone_result(capability)
                self._audit(
                    identity=identity,
                    tool=tool,
                    decision="allow" if result["status"] == "ok" else "unavailable",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error=reason if result["status"] != "ok" else None,
                )
                return result
            result = self.conversation_search(capability, arguments)
            self._audit(
                identity=identity,
                tool=tool,
                decision="allow",
                duration_ms=(time.perf_counter() - started) * 1000,
                hit_count=result["count"],
            )
            return result
        except BrainError as exc:
            self._audit(
                identity=identity,
                tool=tool,
                decision="deny" if not exc.unavailable else "unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=exc.code,
            )
            raise
        # Fail closed and keep technical details out of the wire.
        except Exception as exc:
            logger.exception("brain request failed: %s", type(exc).__name__)
            self._audit(
                identity=identity,
                tool=tool,
                decision="unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error="DB_UNAVAILABLE",
            )
            raise DatabaseUnavailable() from exc

    @staticmethod
    def _public_phone_result(resolution: PhoneResolution) -> dict[str, Any]:
        if resolution.status == "ok":
            return {"status": "ok", "phone": resolution.phone}
        return {"status": "unavailable", "reason": "phone_not_resolved"}

    def _conversation_phone_result(
        self, capability: Capability
    ) -> tuple[dict[str, Any], str]:
        resolution = resolve_phone(
            capability.chat_id, self.settings.whatsapp_session_dir
        )
        return self._public_phone_result(resolution), resolution.reason

    def conversation_phone(self, capability: Capability) -> dict[str, Any]:
        result, _reason = self._conversation_phone_result(capability)
        return result

    def gateway_conversation_phone(
        self,
        headers: Mapping[str, str],
        context: GatewaySessionContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        identity: dict[str, Any] = {
            "profile": "unknown",
            "mode": "unknown",
            "task_id": None,
            "run_id": None,
        }
        try:
            request_identity = self.authorizer.parse_gateway_headers(headers)
            identity["profile"] = request_identity.principal
            identity["mode"] = self.settings.principals[request_identity.principal].mode
            capability = self.authorizer.authorize_gateway(request_identity, context)
            result, reason = self._conversation_phone_result(capability)
            self._audit(
                identity=identity,
                tool="conversation_phone",
                decision="allow" if result["status"] == "ok" else "unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=reason if result["status"] != "ok" else None,
            )
            return result
        except BrainError as exc:
            self._audit(
                identity=identity,
                tool="conversation_phone",
                decision="deny" if not exc.unavailable else "unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=exc.code,
            )
            raise
        except Exception as exc:
            logger.exception("brain gateway request failed: %s", type(exc).__name__)
            self._audit(
                identity=identity,
                tool="conversation_phone",
                decision="unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error="DB_UNAVAILABLE",
            )
            raise DatabaseUnavailable() from exc

    def gateway_conversation_context(
        self,
        headers: Mapping[str, str],
        context: GatewaySessionContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        identity: dict[str, Any] = {
            "profile": "unknown",
            "mode": "unknown",
            "task_id": None,
            "run_id": None,
        }
        try:
            request_identity = self.authorizer.parse_gateway_headers(
                headers, "conversation_context"
            )
            identity["profile"] = request_identity.principal
            identity["mode"] = self.settings.principals[request_identity.principal].mode
            capability = self.authorizer.authorize_gateway(request_identity, context)
            resolution = resolve_phone(
                capability.chat_id, self.settings.whatsapp_session_dir
            )
            if resolution.status != "ok" or not resolution.phone:
                result = {"status": "unavailable", "reason": "contact_not_resolved"}
            elif self.runtime_ids is None:
                raise DatabaseUnavailable()
            else:
                contact_key = self.runtime_ids.contact_key(resolution.phone)
                result = self.runtime.read(
                    lambda conn: self._conversation_context_from_runtime(
                        conn,
                        contact_key=contact_key,
                        phone_e164=resolution.phone,
                        now=time.time(),
                    )
                )
            self._audit(
                identity=identity,
                tool="conversation_context",
                decision="allow" if result["status"] == "ok" else "unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=result.get("reason"),
            )
            return result
        except BrainError as exc:
            self._audit(
                identity=identity,
                tool="conversation_context",
                decision="deny" if not exc.unavailable else "unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=exc.code,
            )
            raise
        except Exception as exc:
            logger.exception("brain context request failed: %s", type(exc).__name__)
            self._audit(
                identity=identity,
                tool="conversation_context",
                decision="unavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
                error="DB_UNAVAILABLE",
            )
            raise DatabaseUnavailable() from exc

    @staticmethod
    def _conversation_context_from_runtime(
        conn: sqlite3.Connection,
        *,
        contact_key: str,
        phone_e164: str,
        now: float,
    ) -> dict[str, Any]:
        """Transport evidence for the contact the CEO is speaking to right now.

        Keyed by contact rather than by Hermes turn. Amendment 2 removed the
        turn-correlation spine, and this contract no longer needs it: the CEO
        asks who is on the other side and whether they arrived from an ad,
        which is a property of the contact's recent transport, not of Hermes'
        internal turn structure.

        The window and count bounds are the contract, not tuning. A contact's
        full transport history is a profile, not context, and section 7
        forbids reading a lifecycle origin out of the oldest CTWA event ever
        seen for a phone. Every event stays transport-level evidence:
        `inbound_kind` is null here and always will be.
        """
        rows = conn.execute(
            "SELECT event_id, transport_kind, source_app FROM transport_events "
            "WHERE contact_key = ? AND received_at > ? "
            "ORDER BY received_at DESC, event_id LIMIT ?",
            (contact_key, now - CONTEXT_WINDOW_SECONDS, CONTEXT_MAX_EVENTS),
        ).fetchall()
        if not rows:
            return {"status": "unavailable", "reason": "no_recent_transport"}
        ephemera = conn.execute(
            "SELECT display_name FROM contact_ephemera "
            "WHERE contact_key = ? AND expires_at > ? AND display_name IS NOT NULL",
            (contact_key, now),
        ).fetchone()
        display_name = str(ephemera["display_name"]) if ephemera else None
        return {
            "status": "ok",
            "contact": {
                "phone_e164": phone_e164,
                "display_name": display_name,
                "display_name_source": "whatsapp_profile" if display_name else None,
            },
            "events": [
                {
                    "event_id": str(row["event_id"]),
                    "transport_kind": str(row["transport_kind"]),
                    "source_app": str(row["source_app"])
                    if row["source_app"] is not None
                    else None,
                    "inbound_kind": None,
                }
                for row in reversed(rows)
            ],
        }

    @staticmethod
    def _validate_limit(
        arguments: Mapping[str, Any], default: int, maximum: int
    ) -> int:
        value = arguments.get("limit", default)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not (1 <= value <= maximum)
        ):
            raise InvalidRequest()
        return value

    def _read_rows(self, capability: Capability) -> list[dict[str, Any]]:
        if not capability.session_ids:
            return []

        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            # SQLite's variable limit is commonly 999. Chunking does not alter
            # authorization: every id came from the same state-db capability.
            for start in range(0, len(capability.session_ids), 800):
                chunk = capability.session_ids[start : start + 800]
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    "SELECT id, session_id, role, content, timestamp, active, compacted, "
                    "display_kind, _compressed_summary, tool_calls, tool_name "
                    f"FROM messages WHERE session_id IN ({placeholders}) "
                    "AND (active = 1 OR compacted = 1) ORDER BY id ASC"
                )
                rows.extend(dict(row) for row in conn.execute(query, chunk).fetchall())
            return rows

        return self.state.read(read)

    def _project(self, capability: Capability) -> list[ProjectedMessage]:
        return project_rows(self._read_rows(capability))

    def _scope_digest(self, capability: Capability) -> str:
        raw = "\0".join(
            (
                capability.profile,
                capability.task_id,
                str(capability.run_id),
                capability.session_key,
            )
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _encode_cursor(self, capability: Capability, before_id: int) -> str:
        payload = json.dumps(
            {
                "v": CURSOR_VERSION,
                "scope": self._scope_digest(capability),
                "before": int(before_id),
            },
            separators=(",", ":"),
        ).encode("ascii")
        tag = hmac.new(self.settings.cursor_secret, payload, hashlib.sha256).digest()[
            :16
        ]
        return (
            base64.urlsafe_b64encode(payload + b"." + tag).decode("ascii").rstrip("=")
        )

    def _decode_cursor(self, capability: Capability, cursor: Any) -> int:
        if not isinstance(cursor, str) or not (1 <= len(cursor) <= 512):
            raise BrainError("CURSOR_INVALID")
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, tag = decoded.rsplit(b".", 1)
            expected = hmac.new(
                self.settings.cursor_secret, payload, hashlib.sha256
            ).digest()[:16]
            if not hmac.compare_digest(tag, expected):
                raise ValueError
            parsed = json.loads(payload.decode("ascii"))
            if (
                parsed.get("v") != CURSOR_VERSION
                or parsed.get("scope") != self._scope_digest(capability)
                or isinstance(parsed.get("before"), bool)
                or not isinstance(parsed.get("before"), int)
                or parsed["before"] <= 0
            ):
                raise ValueError
            return parsed["before"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            raise BrainError("CURSOR_INVALID") from None

    def _render_message(
        self, message: ProjectedMessage, remaining: int
    ) -> tuple[dict[str, Any], int, bool]:
        max_chars = min(self.settings.message_max_chars, max(0, remaining))
        if len(message.text) <= max_chars:
            return (
                {
                    "ref": f"m:{message.message_id}",
                    "speaker": message.speaker,
                    "trust": message.trust,
                    "timestamp": message.timestamp,
                    "text": message.text,
                },
                len(message.text),
                False,
            )
        if max_chars <= 0:
            return ({}, 0, True)
        marker = (
            TRUNCATION_MARKER
            if len(TRUNCATION_MARKER) <= max_chars
            else "…"[:max_chars]
        )
        prefix_length = max(0, max_chars - len(marker))
        text = message.text[:prefix_length] + marker
        return (
            {
                "ref": f"m:{message.message_id}",
                "speaker": message.speaker,
                "trust": message.trust,
                "timestamp": message.timestamp,
                "text": text,
                "truncated": True,
            },
            max_chars,
            True,
        )

    def _render_messages(
        self, messages: list[ProjectedMessage]
    ) -> tuple[list[dict[str, Any]], bool, int]:
        # A recent-history budget must preserve the newest messages. Build the
        # suffix newest-first, then restore chronological order for the wire.
        output_reversed: list[dict[str, Any]] = []
        used = 0
        truncated = False
        for message in reversed(messages):
            rendered, consumed, was_truncated = self._render_message(
                message, self.settings.history_budget_chars - used
            )
            if consumed <= 0:
                truncated = True
                break
            output_reversed.append(rendered)
            used += consumed
            truncated = truncated or was_truncated
            if used >= self.settings.history_budget_chars:
                break
        omitted = len(messages) - len(output_reversed)
        return list(reversed(output_reversed)), truncated or omitted > 0, omitted

    def conversation_recent(
        self, capability: Capability, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(arguments) - {"limit", "cursor"}:
            raise InvalidRequest()
        limit = self._validate_limit(arguments, 20, 50)
        cursor_arg = arguments.get("cursor")
        messages = self._project(capability)
        if "cursor" not in arguments:
            selected = messages[-limit:]
            has_more = len(messages) > limit
        else:
            before_id = self._decode_cursor(capability, cursor_arg)
            older = [message for message in messages if message.message_id < before_id]
            selected = older[-limit:]
            has_more = len(older) > limit
        rendered, truncated, omitted = self._render_messages(selected)
        has_more = has_more or omitted > 0
        oldest_rendered_index = len(selected) - len(rendered)
        next_cursor = (
            self._encode_cursor(capability, selected[oldest_rendered_index].message_id)
            if has_more and rendered
            else None
        )
        result: dict[str, Any] = {
            "history_scope": "authorized_whatsapp_dm",
            "messages": rendered,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "truncated": truncated,
        }
        if not messages:
            result["empty_reason"] = "no_prior_messages"
        return result

    @staticmethod
    def _escape_like(term: str) -> str:
        return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _normalize_search(value: str) -> str:
        return unicodedata.normalize("NFC", value).casefold()

    def _search_candidates(self, capability: Capability, terms: list[str]) -> set[int]:
        if not capability.session_ids:
            return set()

        def read(conn: sqlite3.Connection) -> set[int]:
            matches: set[int] = set()
            # SQLite's built-in lower()/NOCASE are ASCII-only. Non-ASCII terms
            # are verified with Unicode casefold after projection; ASCII terms
            # remain useful as a bounded SQL candidate filter.
            ascii_terms = [term for term in terms if term.isascii()]
            for start in range(0, len(capability.session_ids), 800):
                chunk = capability.session_ids[start : start + 800]
                session_placeholders = ",".join("?" for _ in chunk)
                term_clauses = " AND ".join(
                    "lower(CAST(content AS TEXT)) LIKE ? ESCAPE '\\'"
                    for _ in ascii_terms
                )
                query = (
                    "SELECT id FROM messages WHERE session_id IN ("
                    f"{session_placeholders}) AND (active = 1 OR compacted = 1)"
                )
                if term_clauses:
                    query += f" AND {term_clauses}"
                params: list[Any] = list(chunk)
                params.extend(f"%{self._escape_like(term)}%" for term in ascii_terms)
                matches.update(
                    int(row[0]) for row in conn.execute(query, params).fetchall()
                )
            return matches

        return self.state.read(read)

    def conversation_search(
        self, capability: Capability, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(arguments) - {"query", "limit"}:
            raise InvalidRequest()
        query_value = arguments.get("query")
        limit = self._validate_limit(arguments, 8, 20)
        if not isinstance(query_value, str) or not (1 <= len(query_value) <= 300):
            raise InvalidRequest()
        query = query_value.strip()
        terms = [self._normalize_search(term) for term in query.split()[:8] if term]
        if not terms:
            raise InvalidRequest()
        candidate_ids = self._search_candidates(capability, terms)
        projected = self._project(capability)
        matches = [
            (index, message)
            for index, message in enumerate(projected)
            if message.message_id in candidate_ids
            and all(term in self._normalize_search(message.text) for term in terms)
        ][:limit]

        result_matches: list[dict[str, Any]] = []
        remaining = self.settings.history_budget_chars
        for index, message in matches:
            before = projected[max(0, index - 1) : index]
            after = projected[index + 1 : index + 2]
            rendered_message, consumed, _ = self._render_message(message, remaining)
            remaining -= consumed
            rendered_before: list[dict[str, Any]] = []
            rendered_after: list[dict[str, Any]] = []
            if before and remaining > 0:
                rendered, consumed, _ = self._render_message(before[0], remaining)
                rendered_before.append(rendered)
                remaining -= consumed
            if after and remaining > 0:
                rendered, consumed, _ = self._render_message(after[0], remaining)
                rendered_after.append(rendered)
                remaining -= consumed
            result_matches.append(
                {
                    "message": rendered_message,
                    "before": rendered_before,
                    "after": rendered_after,
                }
            )
            if remaining <= 0:
                break

        return {
            "history_scope": "authorized_whatsapp_dm",
            "query": query,
            "matches": result_matches,
            "count": len(result_matches),
        }
