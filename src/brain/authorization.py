"""Per-call Profile/Task/Run/origin capability reconstruction."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .config import BrainSettings, PrincipalConfig
from .db import ReadOnlyDatabase
from .errors import BrainError

TERMINAL_RUN_STATES = frozenset({"done", "failed", "crashed", "timed_out", "reclaimed"})
_RUN_RE = re.compile(r"^[0-9]{1,20}$")


@dataclass(frozen=True)
class WorkerRequestIdentity:
    principal: str
    task_id: str
    run_id: int

    @property
    def profile(self) -> str:
        """V1 compatibility alias for audit and cursor callers."""
        return self.principal


@dataclass(frozen=True)
class GatewayRequestIdentity:
    principal: str


@dataclass(frozen=True)
class GatewaySessionContext:
    platform: str
    chat_type: str
    chat_id: str
    session_key: str
    session_id: str


@dataclass(frozen=True)
class AuthorizedConversation:
    principal: str
    mode: str
    source: str
    chat_type: str
    chat_id: str
    session_key: str
    session_ids: tuple[str, ...]
    task_id: str | None = None
    run_id: int | None = None

    @property
    def profile(self) -> str:
        """V1 compatibility alias for the authenticated principal."""
        return self.principal


# Internal V1 imports remain valid while callers migrate to the explicit name.
RequestIdentity = WorkerRequestIdentity
Capability = AuthorizedConversation


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _has_placeholder(value: str | None) -> bool:
    return value is not None and "${" in value


def _visible(value: str, maximum: int) -> bool:
    return 0 < len(value) <= maximum and all(
        0x21 <= ord(char) <= 0x7E for char in value
    )


class Authorizer:
    def __init__(
        self, settings: BrainSettings, state: ReadOnlyDatabase, kanban: ReadOnlyDatabase
    ) -> None:
        self.settings = settings
        self.state = state
        self.kanban = kanban

    def _authenticate(self, headers: Mapping[str, str]) -> PrincipalConfig:
        authorization = _header(headers, "Authorization")
        if _has_placeholder(authorization):
            raise BrainError("AUTH_UNRESOLVED_PLACEHOLDER")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise BrainError("AUTH_INVALID_TOKEN")
        token = authorization[7:]
        if not _visible(token, 4096) or any(char.isspace() for char in token):
            raise BrainError("AUTH_INVALID_TOKEN")
        presented_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        principal = next(
            (
                configured
                for configured in self.settings.principals.values()
                if hmac.compare_digest(presented_digest, configured.token_sha256)
            ),
            None,
        )
        if principal is None:
            raise BrainError("AUTH_INVALID_TOKEN")
        return principal

    def parse_worker_headers(self, headers: Mapping[str, str]) -> WorkerRequestIdentity:
        task_header = _header(headers, "X-Hermes-Task")
        run_header = _header(headers, "X-Hermes-Run")

        # This check intentionally precedes token parsing and all DB access.
        if any(_has_placeholder(value) for value in (task_header, run_header)):
            raise BrainError("AUTH_UNRESOLVED_PLACEHOLDER")

        principal = self._authenticate(headers)
        if principal.mode != "worker":
            raise BrainError("AUTH_MODE_MISMATCH")

        if task_header is None or not _visible(task_header, 256):
            raise BrainError("AUTH_TASK_INVALID")
        if run_header is None or not _RUN_RE.fullmatch(run_header):
            raise BrainError("AUTH_RUN_MISMATCH")
        run_id = int(run_header)
        if run_id <= 0:
            raise BrainError("AUTH_RUN_MISMATCH")
        return WorkerRequestIdentity(
            principal=principal.name, task_id=task_header, run_id=run_id
        )

    def parse_gateway_headers(self, headers: Mapping[str, str]) -> GatewayRequestIdentity:
        principal = self._authenticate(headers)
        if principal.mode != "gateway":
            raise BrainError("AUTH_MODE_MISMATCH")
        if "conversation_phone" not in principal.tools:
            raise BrainError("AUTH_TOOL_DENIED")
        return GatewayRequestIdentity(principal=principal.name)

    def authorize_worker(
        self,
        identity: WorkerRequestIdentity,
        audit_identity: dict[str, object] | None = None,
    ) -> Capability:
        def kanban_gate(conn):
            task = conn.execute(
                "SELECT id, assignee, status, current_run_id, session_id "
                "FROM tasks WHERE id = ?",
                (identity.task_id,),
            ).fetchone()
            if task is None:
                raise BrainError("AUTH_TASK_INVALID")
            # Only canonical values loaded from the trusted board are copied
            # into audit output. Presented headers are never logged directly.
            if audit_identity is not None:
                audit_identity["task_id"] = str(task["id"])
                audit_identity["run_id"] = identity.run_id
            if task["assignee"] != identity.principal:
                raise BrainError("AUTH_PROFILE_MISMATCH")
            if task["status"] != "running":
                raise BrainError("AUTH_TASK_NOT_RUNNING")
            if (
                task["current_run_id"] is None
                or int(task["current_run_id"]) != identity.run_id
            ):
                raise BrainError("AUTH_RUN_MISMATCH")
            if not task["session_id"]:
                raise BrainError("AUTH_ORIGIN_MISSING")

            run = conn.execute(
                "SELECT id, task_id, status FROM task_runs WHERE id = ?",
                (identity.run_id,),
            ).fetchone()
            if run is None or run["task_id"] != identity.task_id:
                raise BrainError("AUTH_RUN_MISMATCH")
            if str(run["status"]).lower() in TERMINAL_RUN_STATES:
                raise BrainError("AUTH_RUN_TERMINAL")

            subscriptions = conn.execute(
                "SELECT task_id, platform, chat_id, chat_type, notifier_profile "
                "FROM kanban_notify_subs WHERE task_id = ? AND platform = 'whatsapp'",
                (identity.task_id,),
            ).fetchall()
            if not subscriptions:
                raise BrainError("AUTH_ORIGIN_MISSING")
            if any(
                row["task_id"] != identity.task_id
                or row["platform"] != "whatsapp"
                or not row["chat_id"]
                or row["chat_type"] != "dm"
                or row["notifier_profile"] != "default"
                for row in subscriptions
            ):
                raise BrainError("AUTH_ORIGIN_MISSING")
            chat_ids = {str(row["chat_id"]) for row in subscriptions}
            if len(chat_ids) != 1:
                raise BrainError("AUTH_ORIGIN_AMBIGUOUS")
            return str(task["session_id"]), next(iter(chat_ids))

        task_session_id, chat_id = self.kanban.read(kanban_gate)

        def state_gate(conn):
            sessions = conn.execute(
                "SELECT id, session_key, source, chat_id, chat_type, started_at "
                "FROM sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
            if not sessions:
                raise BrainError("AUTH_ORIGIN_MISSING")
            if any(
                row["source"] != "whatsapp" or row["chat_type"] != "dm"
                for row in sessions
            ):
                raise BrainError("SCOPE_NOT_WHATSAPP_DM")
            session_keys = {
                str(row["session_key"]) for row in sessions if row["session_key"]
            }
            if len(session_keys) > 1:
                raise BrainError("AUTH_ORIGIN_AMBIGUOUS_ALIAS")
            if not session_keys:
                raise BrainError("SCOPE_NOT_WHATSAPP_DM")
            session_key = next(iter(session_keys))
            longitudinal = conn.execute(
                "SELECT id FROM sessions "
                "WHERE session_key = ? AND source = 'whatsapp' AND chat_type = 'dm' "
                "ORDER BY started_at ASC, id ASC",
                (session_key,),
            ).fetchall()
            ids = tuple(str(row["id"]) for row in longitudinal)
            if not ids or task_session_id not in ids:
                raise BrainError("AUTH_SESSION_MISMATCH")
            return session_key, ids

        session_key, session_ids = self.state.read(state_gate)
        return AuthorizedConversation(
            principal=identity.principal,
            mode="worker",
            source="whatsapp",
            chat_type="dm",
            chat_id=chat_id,
            task_id=identity.task_id,
            run_id=identity.run_id,
            session_key=session_key,
            session_ids=session_ids,
        )

    def authorize_gateway(
        self,
        identity: GatewayRequestIdentity,
        context: GatewaySessionContext,
    ) -> AuthorizedConversation:
        def state_gate(conn):
            session = conn.execute(
                "SELECT id, session_key, source, chat_id, chat_type "
                "FROM sessions WHERE id = ?",
                (context.session_id,),
            ).fetchone()
            if session is None:
                raise BrainError("AUTH_SESSION_MISMATCH")
            if (
                session["source"] != context.platform
                or session["chat_type"] != context.chat_type
                or session["chat_id"] != context.chat_id
                or session["session_key"] != context.session_key
            ):
                raise BrainError("AUTH_SESSION_MISMATCH")

            longitudinal = conn.execute(
                "SELECT id, session_key, source, chat_id, chat_type "
                "FROM sessions WHERE session_key = ? "
                "ORDER BY started_at ASC, id ASC",
                (session["session_key"],),
            ).fetchall()
            if not longitudinal:
                raise BrainError("AUTH_ORIGIN_MISSING")
            if any(
                row["source"] != "whatsapp" or row["chat_type"] != "dm"
                for row in longitudinal
            ):
                raise BrainError("SCOPE_NOT_WHATSAPP_DM")
            if any(row["chat_id"] != session["chat_id"] for row in longitudinal):
                raise BrainError("AUTH_ORIGIN_AMBIGUOUS_ALIAS")
            return tuple(str(row["id"]) for row in longitudinal)

        session_ids = self.state.read(state_gate)
        return AuthorizedConversation(
            principal=identity.principal,
            mode="gateway",
            source="whatsapp",
            chat_type="dm",
            chat_id=str(context.chat_id),
            session_key=str(context.session_key),
            session_ids=session_ids,
        )

    def parse_headers(self, headers: Mapping[str, str]) -> WorkerRequestIdentity:
        """V1 compatibility alias for the explicit worker parser."""
        return self.parse_worker_headers(headers)

    def authorize_identity(
        self,
        identity: WorkerRequestIdentity,
        audit_identity: dict[str, object] | None = None,
    ) -> Capability:
        """V1 compatibility alias for worker authorization."""
        return self.authorize_worker(identity, audit_identity)

    def authorize(
        self,
        headers: Mapping[str, str],
        audit_identity: dict[str, object] | None = None,
    ) -> Capability:
        identity = self.parse_worker_headers(headers)
        if audit_identity is not None:
            audit_identity["profile"] = identity.principal
        return self.authorize_worker(identity, audit_identity)
