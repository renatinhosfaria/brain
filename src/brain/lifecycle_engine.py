"""Deterministic lifecycle engine: bind one CTWA origin to one exact client.

Spec sections 10.3 and 14. A lifecycle exists only when every link in the chain
is proven independently — a correlated WhatsApp turn, exactly one CTWA
candidate inside it, a terminal Cadastro run that created exactly one client —
and it is never inferred from chronology.

Nothing here writes to FamaChat. The engine decides what state is desired; the
writer is a separate service holding the only credential that can apply it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from .hermes_evidence import BoundTask, TerminalRun
from .lifecycle_models import (
    BIND_CONFLICT,
    BIND_CREATED,
    BIND_NOOP,
    BIND_SKIPPED,
    CREATING_DECISION,
    FACT_CLIENT_CREATED,
    FACT_FIRST_HUMAN_INBOUND,
    PHASE_ACTIVE,
    TRANSPORT_CTWA,
    TRANSPORT_ORDINARY,
    BindResult,
)
from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds

CORRELATED = "correlated"
KANBAN_WATERMARK = "kanban_run_watermark"
STATUS_SEM_ATENDIMENTO = "Sem Atendimento"


def _positive_client_id(value: object) -> int | None:
    """Accept a client id only in the exact shape the worker contract emits.

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, and
    ``True`` would otherwise bind a lifecycle to client 1.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class LifecycleEngine:
    def __init__(
        self,
        runtime: RuntimeDatabase,
        runtime_ids: RuntimeIds,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime = runtime
        self.runtime_ids = runtime_ids
        self.clock = clock

    # ------------------------------------------------------------------

    def kanban_watermark(self) -> int:
        def read(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT value FROM reconcile_state WHERE name = ?",
                (KANBAN_WATERMARK,),
            ).fetchone()
            if row is None:
                return 0
            try:
                return int(str(row["value"]))
            except (TypeError, ValueError):
                return 0

        return self.runtime.read(read)

    def bind_completed_cadastro(self, task: BoundTask, run: TerminalRun) -> BindResult:
        """Create at most one lifecycle from one terminal Cadastro run."""
        now = self.clock()
        run_id = run.run_id

        if run.decision != CREATING_DECISION:
            # JA_E_CLIENTE, CORRETOR_ATIVO, INCONCLUSIVO and anything ambiguous
            # legitimately create nothing (spec 10.3).
            return self._settle(
                run_id, now, BindResult(BIND_SKIPPED, reason="decision")
            )

        client_id = _positive_client_id(run.entities.get("client_id"))
        if client_id is None:
            return self._settle(
                run_id, now, BindResult(BIND_SKIPPED, reason="client_id")
            )

        def write(conn: sqlite3.Connection) -> BindResult:
            turn = conn.execute(
                "SELECT contact_key, correlation_status FROM whatsapp_turns "
                "WHERE wa_turn_id = ?",
                (task.wa_turn_id,),
            ).fetchone()
            if turn is None or str(turn["correlation_status"]) != CORRELATED:
                # Premise P7: a card carrying an internal re-invocation's turn
                # has no transport origin and never will.
                return BindResult(BIND_SKIPPED, reason="turn_not_correlated")

            contact_key = str(turn["contact_key"] or "")
            origins = conn.execute(
                "SELECT event.event_id, event.message_timestamp, event.received_at "
                "FROM turn_events AS binding "
                "JOIN transport_events AS event ON event.event_id = binding.event_id "
                "WHERE binding.wa_turn_id = ? AND event.transport_kind = ? "
                "ORDER BY binding.ordinal",
                (task.wa_turn_id, TRANSPORT_CTWA),
            ).fetchall()
            if len(origins) != 1:
                # Zero means the turn is not ad-originated. More than one is
                # ambiguous, and spec 7.3 forbids picking the likeliest.
                return BindResult(BIND_SKIPPED, reason="origin_not_unique")

            origin = origins[0]
            origin_event_id = str(origin["event_id"])
            origin_at = origin["message_timestamp"] or origin["received_at"]

            existing = conn.execute(
                "SELECT lifecycle_id, client_id FROM lead_lifecycles "
                "WHERE origin_event_id = ?",
                (origin_event_id,),
            ).fetchone()
            if existing is not None:
                if int(existing["client_id"]) == client_id:
                    return BindResult(
                        BIND_NOOP, lifecycle_id=str(existing["lifecycle_id"])
                    )
                return BindResult(
                    BIND_CONFLICT,
                    lifecycle_id=str(existing["lifecycle_id"]),
                    reason="origin_bound_to_another_client",
                )

            lifecycle_id = self.runtime_ids.effect_id(
                "lifecycle", origin_event_id, str(client_id)
            )
            conn.execute(
                "INSERT INTO lead_lifecycles (lifecycle_id, origin_event_id, "
                "wa_turn_id, contact_key, client_id, phase, last_proven_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lifecycle_id,
                    origin_event_id,
                    task.wa_turn_id,
                    contact_key,
                    client_id,
                    PHASE_ACTIVE,
                    STATUS_SEM_ATENDIMENTO,
                    now,
                    now,
                ),
            )
            self._record_fact(
                conn,
                lifecycle_id,
                FACT_CLIENT_CREATED,
                evidence_ref=f"run:{run_id}",
                observed_at=run.ended_at or now,
                now=now,
            )
            self._materialise_arrived_human(
                conn, lifecycle_id, contact_key, origin_event_id, origin_at, now
            )
            return BindResult(BIND_CREATED, lifecycle_id=lifecycle_id)

        return self._settle(run_id, now, self.runtime.write(write))

    # ------------------------------------------------------------------

    def _materialise_arrived_human(
        self,
        conn: sqlite3.Connection,
        lifecycle_id: str,
        contact_key: str,
        origin_event_id: str,
        origin_at: float | None,
        now: float,
    ) -> None:
        """Catch a human reply that arrived before Cadastro finished.

        Spec section 14: facts are derived from evidence, not from the order in
        which Brain happened to learn them. A lead who answered while the
        registration was still running is already ``Em Atendimento``.
        """
        if origin_at is None:
            return
        row = conn.execute(
            "SELECT event_id, COALESCE(message_timestamp, received_at) AS at "
            "FROM transport_events "
            "WHERE contact_key = ? AND transport_kind = ? AND event_id != ? "
            "AND COALESCE(message_timestamp, received_at) > ? "
            "ORDER BY at, event_id LIMIT 1",
            (contact_key, TRANSPORT_ORDINARY, origin_event_id, origin_at),
        ).fetchone()
        if row is None:
            return
        self._record_fact(
            conn,
            lifecycle_id,
            FACT_FIRST_HUMAN_INBOUND,
            evidence_ref=str(row["event_id"]),
            observed_at=float(row["at"]),
            now=now,
        )

    @staticmethod
    def _record_fact(
        conn: sqlite3.Connection,
        lifecycle_id: str,
        fact_type: str,
        *,
        evidence_ref: str,
        observed_at: float,
        now: float,
    ) -> None:
        """Facts are immutable per lifecycle and type; the first one wins."""
        conn.execute(
            "INSERT OR IGNORE INTO lifecycle_facts (lifecycle_id, fact_type, "
            "evidence_ref, observed_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (lifecycle_id, fact_type, evidence_ref, observed_at, now),
        )

    def _settle(self, run_id: int, now: float, result: BindResult) -> BindResult:
        """Advance the watermark only once the run has been fully processed."""

        def write(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value FROM reconcile_state WHERE name = ?",
                (KANBAN_WATERMARK,),
            ).fetchone()
            try:
                current = int(str(row["value"])) if row is not None else 0
            except (TypeError, ValueError):
                current = 0
            if run_id <= current:
                return
            conn.execute(
                "INSERT INTO reconcile_state (name, value, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (KANBAN_WATERMARK, str(run_id), now),
            )

        self.runtime.write(write)
        return result
