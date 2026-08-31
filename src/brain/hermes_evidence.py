"""Read-only readers for the Hermes evidence the lifecycle engine depends on.

Every query here goes through ``ReadOnlyDatabase``, which opens Hermes' own
databases with ``mode=ro`` and ``PRAGMA query_only=ON``. Spec section 2.3 makes
that mandatory: Brain decides what lifecycle state is desired, but it is never
the writer of Hermes state.

Malformed evidence resolves to absent rather than to a partial reading. A
lifecycle built on half-parsed metadata is worse than one that never starts,
because the failure is silent and the CRM ends up wrong.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .db import ReadOnlyDatabase
from .errors import DatabaseUnavailable

APPROVED_STAGES = frozenset({"porteiro", "cadastro", "reno"})
TERMINAL_RUN_STATES = frozenset({"done", "failed", "crashed", "timed_out", "reclaimed"})
DELIVERED_STATE = "delivered"

_IDEMPOTENCY_RE = re.compile(r"^whatsapp:(waturn_[0-9a-f]{64}):([a-z]+)$")


def parse_whatsapp_idempotency_key(value: object) -> tuple[str, str] | None:
    """Return ``(wa_turn_id, stage)`` for a well-formed WhatsApp key.

    The returned turn is the origin turn of spec 10.1.1: all three stage cards
    of one lead carry it. Anything else — a phone-derived key, an unknown
    stage, a placeholder such as ``context-unavailable`` — is not a binding and
    must not become one.
    """
    if not isinstance(value, str):
        return None
    match = _IDEMPOTENCY_RE.fullmatch(value)
    if match is None:
        return None
    wa_turn_id, stage = match.group(1), match.group(2)
    if stage not in APPROVED_STAGES:
        return None
    return wa_turn_id, stage


@dataclass(frozen=True)
class BoundTask:
    task_id: str
    assignee: str
    status: str
    current_run_id: int | None
    session_id: str | None
    wa_turn_id: str
    stage: str


@dataclass(frozen=True)
class TerminalRun:
    run_id: int
    task_id: str
    status: str
    summary: str
    started_at: float | None
    ended_at: float | None
    decision: str | None = None
    entities: dict[str, Any] = field(default_factory=dict)
    response_ready: str | None = None


@dataclass(frozen=True)
class DeliveredObligation:
    obligation_id: str
    session_key: str
    content: str
    state: str
    created_at: float | None
    updated_at: float | None


def _timestamp(value: object) -> float | None:
    """Normalise every Hermes timestamp through one place."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def _structured_metadata(raw: object) -> dict[str, Any]:
    """Parse run metadata only when it is a JSON object.

    A string, an array, or a scalar is not the structured shape the worker
    contract produces, so it yields nothing rather than being coerced.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class HermesEvidenceReader:
    """Bounded, typed access to the Hermes rows the lifecycle engine reads."""

    def __init__(self, state: ReadOnlyDatabase, kanban: ReadOnlyDatabase) -> None:
        self.state = state
        self.kanban = kanban

    def list_bound_tasks(self, *, after_run_id: int = 0) -> list[BoundTask]:
        """Tasks whose idempotency key binds them to a WhatsApp origin turn."""

        def read(conn: sqlite3.Connection) -> list[BoundTask]:
            rows = conn.execute(
                "SELECT id, assignee, status, current_run_id, session_id, "
                "idempotency_key FROM tasks "
                "WHERE idempotency_key LIKE 'whatsapp:%' "
                "AND COALESCE(current_run_id, 0) > ? "
                "ORDER BY COALESCE(current_run_id, 0), id",
                (after_run_id,),
            ).fetchall()
            bound: list[BoundTask] = []
            for row in rows:
                parsed = parse_whatsapp_idempotency_key(row["idempotency_key"])
                if parsed is None:
                    continue
                wa_turn_id, stage = parsed
                bound.append(
                    BoundTask(
                        task_id=str(row["id"]),
                        assignee=str(row["assignee"] or ""),
                        status=str(row["status"] or ""),
                        current_run_id=row["current_run_id"],
                        session_id=row["session_id"],
                        wa_turn_id=wa_turn_id,
                        stage=stage,
                    )
                )
            return bound

        return self._read(self.kanban, read, [])

    def terminal_run(self, task_id: str, run_id: int) -> TerminalRun | None:
        """The run for this task, only once it has actually finished."""

        def read(conn: sqlite3.Connection) -> TerminalRun | None:
            row = conn.execute(
                "SELECT id, task_id, status, summary, metadata, started_at, ended_at "
                "FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"] or "")
            ended_at = _timestamp(row["ended_at"])
            if status not in TERMINAL_RUN_STATES or ended_at is None:
                return None
            metadata = _structured_metadata(row["metadata"])
            entities = metadata.get("entities")
            decision = metadata.get("decision")
            response_ready = metadata.get("response_ready")
            return TerminalRun(
                run_id=int(row["id"]),
                task_id=str(row["task_id"]),
                status=status,
                summary=str(row["summary"] or ""),
                started_at=_timestamp(row["started_at"]),
                ended_at=ended_at,
                decision=decision if isinstance(decision, str) else None,
                entities=entities if isinstance(entities, dict) else {},
                response_ready=(
                    response_ready if isinstance(response_ready, str) else None
                ),
            )

        return self._read(self.kanban, read, None)

    def delivered_obligations(
        self, session_key: str, *, since: float
    ) -> list[DeliveredObligation]:
        """Obligations Hermes marked delivered for this session after ``since``.

        Spec section 15: for WhatsApp, ``state='delivered'`` means Hermes marked
        the final outbound after a successful send. It is not a read receipt and
        not two ticks.
        """

        def read(conn: sqlite3.Connection) -> list[DeliveredObligation]:
            rows = conn.execute(
                "SELECT obligation_id, session_key, content, state, created_at, "
                "updated_at FROM delivery_obligations "
                "WHERE session_key = ? AND state = ? AND updated_at >= ? "
                "ORDER BY updated_at, obligation_id",
                (session_key, DELIVERED_STATE, since),
            ).fetchall()
            return [
                DeliveredObligation(
                    obligation_id=str(row["obligation_id"]),
                    session_key=str(row["session_key"]),
                    content=str(row["content"] or ""),
                    state=str(row["state"]),
                    created_at=_timestamp(row["created_at"]),
                    updated_at=_timestamp(row["updated_at"]),
                )
                for row in rows
            ]

        return self._read(self.state, read, [])

    @staticmethod
    def _read(db: ReadOnlyDatabase, callback, fallback):
        """An unreadable Hermes database degrades lifecycle, never crashes it."""
        try:
            return db.read(callback)
        except (DatabaseUnavailable, sqlite3.Error):
            return fallback
