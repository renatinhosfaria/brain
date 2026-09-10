"""Operator-issued, single-run resumption grants; never a model-facing tool.

The local administrator attests an already authenticated control authorization.
Subscriptions constrain the destination; they cannot issue a grant. Hermes
stores remain read-only and the original task session is never rewritten.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import NoReturn

from .authorization import Authorizer, GatewaySessionContext
from .config import BrainSettings
from .db import ReadOnlyDatabase
from .errors import BrainError
from .runtime_db import RuntimeDatabase

logger = logging.getLogger("brain.audit")
_SCHEMA = """CREATE TABLE IF NOT EXISTS conversation_resumptions (
    task_id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL UNIQUE,
    binding TEXT NOT NULL,
    run_watermark INTEGER NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    bound_run_id INTEGER,
    claimed_at REAL,
    revoked_at REAL
)"""


def deny() -> NoReturn:
    raise BrainError("AUTH_RESUMPTION_DENIED")


class ResumptionRegistry:
    def __init__(self, settings: BrainSettings):
        self.settings = settings
        self.kanban = ReadOnlyDatabase(settings.kanban_db)
        self.state = ReadOnlyDatabase(settings.state_db)
        self.runtime = RuntimeDatabase(
            settings.runtime_db, settings.busy_timeout_seconds
        )

    def inspect(self, task_id: str, parent_id: str, *, identity=None):
        """Validate metadata only. No history, runtime initialization or writes."""

        def board(conn):
            task = conn.execute(
                "SELECT id, assignee, status, current_run_id, session_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            parent = conn.execute(
                "SELECT id, assignee, status, current_run_id, session_id FROM tasks WHERE id=?",
                (parent_id,),
            ).fetchone()
            edge = conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                (parent_id, task_id),
            ).fetchone()
            if (
                task is None
                or parent is None
                or edge is None
                or parent["status"] != "done"
            ):
                deny()
            principal = self.settings.principals.get(task["assignee"])
            if principal is None or principal.mode != "worker":
                deny()
            if identity is None:
                if task["status"] != "blocked" or task["current_run_id"] is not None:
                    deny()
            else:
                if (
                    task["status"] != "running"
                    or task["assignee"] != identity.principal
                    or task["current_run_id"] != identity.run_id
                    or task_id != identity.task_id
                ):
                    deny()
                run = conn.execute(
                    "SELECT id, task_id, status FROM task_runs WHERE id=?",
                    (identity.run_id,),
                ).fetchone()
                if (
                    run is None
                    or run["task_id"] != task_id
                    or run["status"] != "running"
                ):
                    deny()
            destinations = []
            for tid in (task_id, parent_id):
                rows = conn.execute(
                    "SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform='whatsapp'",
                    (tid,),
                ).fetchall()
                if (
                    not rows
                    or any(
                        r["chat_type"] != "dm"
                        or r["notifier_profile"] != "default"
                        or not r["chat_id"]
                        for r in rows
                    )
                    or len({r["chat_id"] for r in rows}) != 1
                ):
                    deny()
                destinations.append(rows[0]["chat_id"])
            if destinations[0] != destinations[1]:
                deny()
            watermark = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_runs WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            return dict(task), dict(parent), destinations[0], watermark

        task, parent, chat_id, watermark = self.kanban.read(board)

        def sessions(conn):
            control = conn.execute(
                "SELECT id, session_key, source, chat_id, chat_type FROM sessions WHERE id=?",
                (task["session_id"],),
            ).fetchone()
            origin = conn.execute(
                "SELECT id, session_key, source, chat_id, chat_type FROM sessions WHERE id=?",
                (parent["session_id"],),
            ).fetchone()
            if (
                control is None
                or control["source"] != "telegram"
                or not control["session_key"]
            ):
                deny()
            if (
                origin is None
                or origin["source"] != "whatsapp"
                or origin["chat_type"] != "dm"
                or origin["chat_id"] != chat_id
            ):
                deny()
            return dict(control), dict(origin)

        control, origin = self.state.read(sessions)
        # Reuse the existing longitudinal/alias proof without manufacturing a
        # gateway principal. All context fields come from the stored parent.
        Authorizer(
            self.settings, self.state, self.kanban
        ).validated_gateway_session_ids(
            GatewaySessionContext(
                "whatsapp", "dm", origin["chat_id"], origin["session_key"], origin["id"]
            ),
        )
        binding = {
            "task_id": task_id,
            "parent_id": parent_id,
            "principal": task["assignee"],
            "control_session_id": control["id"],
            "control_session_key": control["session_key"],
            "control_chat_id": control["chat_id"],
            "control_chat_type": control["chat_type"],
            "context_session_id": origin["id"],
            "context_session_key": origin["session_key"],
            "context_chat_id": origin["chat_id"],
        }
        return binding, watermark

    def issue(
        self, task_id: str, parent_id: str, authorization_id: str
    ) -> dict[str, object]:
        """Trusted local-admin API. Not exported over HTTP or MCP.

        The UUID is an audit reference, NOT a token or proof of Telegram identity.
        Calling this method is the administrator's explicit attestation.
        """
        try:
            if str(uuid.UUID(authorization_id)) != authorization_id:
                deny()
        except (ValueError, TypeError, AttributeError):
            deny()
        binding, watermark = self.inspect(task_id, parent_id)
        encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
        now = time.time()

        def write(conn):
            conn.execute(_SCHEMA)
            row = conn.execute(
                "SELECT * FROM conversation_resumptions WHERE task_id=? OR authorization_id=?",
                (task_id, authorization_id),
            ).fetchone()
            if row is not None:
                if (
                    row["task_id"] != task_id
                    or row["authorization_id"] != authorization_id
                    or row["binding"] != encoded
                    or row["bound_run_id"] is not None
                    or row["revoked_at"] is not None
                    or row["expires_at"] <= now
                ):
                    deny()
                return "already_issued"
            # Revalidate after taking the writer lock; don't persist a stale
            # task/parent snapshot if the operator raced a dispatcher mutation.
            current, current_watermark = self.inspect(task_id, parent_id)
            if current != binding or current_watermark != watermark:
                deny()
            conn.execute(
                "INSERT INTO conversation_resumptions (task_id, authorization_id, binding, run_watermark, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, authorization_id, encoded, watermark, now, now + 3600),
            )
            return "issued"

        result = self.runtime.write(write)
        logger.info(
            "resumption action=%s task_id=%s authorization_id=%s actor=local_admin",
            result,
            task_id,
            authorization_id,
        )
        return {
            "status": result,
            "task_id": task_id,
            "authorization_id": authorization_id,
        }

    def resolve(self, identity, control_session_id: str, chat_id: str) -> str:
        """Claim once atomically; revalidate all evidence on every worker call."""

        def claim(conn):
            row = conn.execute(
                "SELECT * FROM conversation_resumptions WHERE task_id=?",
                (identity.task_id,),
            ).fetchone()
            now = time.time()
            if (
                row is None
                or row["revoked_at"] is not None
                or row["expires_at"] <= now
                or identity.run_id <= row["run_watermark"]
                or row["bound_run_id"] not in (None, identity.run_id)
            ):
                deny()
            first_run = self.kanban.read(
                lambda board: board.execute(
                    "SELECT id, started_at FROM task_runs WHERE task_id=? AND id>? ORDER BY id LIMIT 1",
                    (identity.task_id, row["run_watermark"]),
                ).fetchone()
            )
            if (
                first_run is None
                or first_run["id"] != identity.run_id
                or first_run["started_at"] is None
                # Hermes stores run timestamps in whole seconds. The monotonic
                # run watermark above proves ordering within the same second.
                or first_run["started_at"] < int(row["issued_at"])
            ):
                deny()
            saved = json.loads(row["binding"])
            binding, _ = self.inspect(
                identity.task_id, saved["parent_id"], identity=identity
            )
            if (
                binding != saved
                or binding["principal"] != identity.principal
                or binding["control_session_id"] != control_session_id
                or binding["context_chat_id"] != chat_id
            ):
                deny()
            if row["bound_run_id"] is None:
                conn.execute(
                    "UPDATE conversation_resumptions SET bound_run_id=?, claimed_at=? WHERE task_id=?",
                    (identity.run_id, now, identity.task_id),
                )
            return binding["context_session_id"]

        try:
            # An absent grant database/table must not create runtime state just
            # because an unauthorized worker asks for history.
            if not self.runtime.path.is_file():
                deny()
            return self.runtime.write(claim)
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise BrainError("AUTH_RESUMPTION_DENIED") from None

    def status(self, task_id: str) -> dict[str, object]:
        def read(conn) -> dict[str, object]:
            row = conn.execute(
                "SELECT task_id, authorization_id, issued_at, expires_at, bound_run_id, revoked_at FROM conversation_resumptions WHERE task_id=?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else {"task_id": task_id, "status": "absent"}

        return ReadOnlyDatabase(self.runtime.path).read(read)

    def revoke(self, task_id: str):
        def write(conn):
            changed = conn.execute(
                "UPDATE conversation_resumptions SET revoked_at=COALESCE(revoked_at, ?) WHERE task_id=?",
                (time.time(), task_id),
            ).rowcount
            if changed != 1:
                deny()

        self.runtime.write(write)
        logger.info("resumption action=revoked task_id=%s actor=local_admin", task_id)
        return {"task_id": task_id, "status": "revoked"}
