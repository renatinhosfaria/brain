"""Deterministic lifecycle engine: bind one CTWA origin to one exact client.

Spec sections 10.3 and 14. A lifecycle exists only when every link in the chain
is proven independently — a correlated WhatsApp turn, exactly one CTWA
candidate inside it, a terminal Cadastro run that created exactly one client —
and it is never inferred from chronology.

Nothing here writes to FamaChat. The engine decides what state is desired; the
writer is a separate service holding the only credential that can apply it.
"""

from __future__ import annotations

import hmac
import sqlite3
import time
from collections.abc import Callable, Sequence

from .hermes_evidence import BoundTask, DeliveredObligation, TerminalRun
from .lifecycle_models import (
    BIND_CONFLICT,
    BIND_CREATED,
    BIND_NOOP,
    BIND_SKIPPED,
    CREATING_DECISION,
    FACT_CLIENT_CREATED,
    FACT_FIRST_HUMAN_INBOUND,
    FACT_FIRST_T1_SEND_SUCCESS,
    PHASE_ACTIVE,
    TRANSPORT_CTWA,
    TRANSPORT_ORDINARY,
    BindResult,
    DeliveryMatch,
)
from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds
from .turn_correlation import session_key_hmac

CORRELATED = "correlated"
DELIVERED_STATE = "delivered"
PROVEN = "proven"
NOT_PROVEN = "not_proven"
AMBIGUOUS = "ambiguous"
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
        runtime_secret: bytes = b"",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime = runtime
        self.runtime_ids = runtime_ids
        self.runtime_secret = bytes(runtime_secret)
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

    def observe_transport_event(self, event_id: str) -> bool:
        """Turn one newly ingested event into a human-inbound fact, at most once.

        Called after transport ingestion has already committed. Spec section 14
        wants the fact derived from evidence, so this only reads what is stored
        and never trusts the caller about what the event is.
        """
        now = self.clock()

        def write(conn: sqlite3.Connection) -> bool:
            event = conn.execute(
                "SELECT contact_key, transport_kind, "
                "COALESCE(message_timestamp, received_at) AS at "
                "FROM transport_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if event is None or str(event["transport_kind"]) != TRANSPORT_ORDINARY:
                # A second ad-attributed message is attribution evidence, never
                # a reply (spec 7.3).
                return False
            return self._claim_human_fact(
                conn,
                contact_key=str(event["contact_key"] or ""),
                event_id=event_id,
                observed_at=event["at"],
                now=now,
            )

        return self.runtime.write(write)

    def repair_human_inbound_facts(self) -> int:
        """Create human facts that ingestion missed. Returns how many were added.

        Ingestion deliberately does not block on lifecycle work: an event is
        acknowledged even when the callback raises. Reconciliation is what makes
        that safe rather than lossy.
        """
        now = self.clock()

        def write(conn: sqlite3.Connection) -> int:
            pending = conn.execute(
                "SELECT lifecycle.lifecycle_id, lifecycle.contact_key, "
                "COALESCE(origin.message_timestamp, origin.received_at) AS origin_at "
                "FROM lead_lifecycles AS lifecycle "
                "JOIN transport_events AS origin "
                "ON origin.event_id = lifecycle.origin_event_id "
                "WHERE lifecycle.phase = ? AND NOT EXISTS ("
                "  SELECT 1 FROM lifecycle_facts AS fact "
                "  WHERE fact.lifecycle_id = lifecycle.lifecycle_id "
                "  AND fact.fact_type = ?)",
                (PHASE_ACTIVE, FACT_FIRST_HUMAN_INBOUND),
            ).fetchall()
            repaired = 0
            for lifecycle in pending:
                row = conn.execute(
                    "SELECT event_id, COALESCE(message_timestamp, received_at) AS at "
                    "FROM transport_events "
                    "WHERE contact_key = ? AND transport_kind = ? "
                    "AND COALESCE(message_timestamp, received_at) > ? "
                    "ORDER BY at, event_id LIMIT 1",
                    (
                        lifecycle["contact_key"],
                        TRANSPORT_ORDINARY,
                        lifecycle["origin_at"],
                    ),
                ).fetchone()
                if row is None:
                    continue
                self._record_fact(
                    conn,
                    str(lifecycle["lifecycle_id"]),
                    FACT_FIRST_HUMAN_INBOUND,
                    evidence_ref=str(row["event_id"]),
                    observed_at=float(row["at"]),
                    now=now,
                )
                repaired += 1
            return repaired

        return self.runtime.write(write)

    def prove_first_t1_send(
        self,
        lifecycle_id: str,
        reno_run: TerminalRun,
        obligations: Sequence[DeliveredObligation],
    ) -> DeliveryMatch:
        """Prove the first outbound of this lifecycle actually left Hermes.

        Spec section 15. The proof is one delivered obligation whose content is
        identical to the Reno run's response_ready, on the same authorised
        session, no earlier than the run that produced it. Zero matches or
        several indistinguishable ones are NOT_PROVEN and must not move the CRM.

        Content is compared through the body HMAC and only in memory. Neither
        the reply nor the ledger row is copied into Brain's database or logs.
        """
        now = self.clock()
        response_ready = reno_run.response_ready
        if not isinstance(response_ready, str) or not response_ready.strip():
            return DeliveryMatch(NOT_PROVEN, reason="no_response_ready")
        if not self.runtime_secret:
            return DeliveryMatch(NOT_PROVEN, reason="no_runtime_secret")

        expected_body = self.runtime_ids.body_hmac(response_ready)
        earliest = reno_run.started_at or reno_run.ended_at

        def write(conn: sqlite3.Connection) -> DeliveryMatch:
            row = conn.execute(
                "SELECT turn.session_key_hmac AS commitment "
                "FROM lead_lifecycles AS lifecycle "
                "JOIN whatsapp_turns AS turn "
                "ON turn.wa_turn_id = lifecycle.wa_turn_id "
                "WHERE lifecycle.lifecycle_id = ?",
                (lifecycle_id,),
            ).fetchone()
            if row is None or not row["commitment"]:
                return DeliveryMatch(NOT_PROVEN, reason="no_session_commitment")
            commitment = str(row["commitment"])

            matches = [
                obligation
                for obligation in obligations
                if self._delivery_matches(
                    obligation, commitment, expected_body, earliest
                )
            ]
            if not matches:
                return DeliveryMatch(NOT_PROVEN, reason="no_match")
            if len(matches) > 1:
                return DeliveryMatch(AMBIGUOUS, reason="multiple_matches")

            matched = matches[0]
            self._record_fact(
                conn,
                lifecycle_id,
                FACT_FIRST_T1_SEND_SUCCESS,
                evidence_ref=matched.obligation_id,
                observed_at=matched.updated_at or now,
                now=now,
            )
            return DeliveryMatch(
                PROVEN,
                obligation_id=matched.obligation_id,
                delivered_at=matched.updated_at,
            )

        return self.runtime.write(write)

    def _delivery_matches(
        self,
        obligation: DeliveredObligation,
        commitment: str,
        expected_body: str,
        earliest: float | None,
    ) -> bool:
        if obligation.state != DELIVERED_STATE or not obligation.session_key:
            return False
        if not hmac.compare_digest(
            session_key_hmac(self.runtime_secret, obligation.session_key), commitment
        ):
            return False
        if earliest is not None and (
            obligation.updated_at is None or obligation.updated_at < earliest
        ):
            # An outbound predating the run cannot be what the run produced.
            return False
        return hmac.compare_digest(
            self.runtime_ids.body_hmac(obligation.content), expected_body
        )

    def lifecycles_with_delivery_proof_at_risk(
        self, *, now: float | None = None, retention_seconds: float = 7 * 86400
    ) -> list[str]:
        """Active lifecycles whose delivery evidence is about to expire upstream.

        Hermes keeps delivery obligations for a bounded period. Once the row is
        gone the send can never be proven, and spec 15 forbids inferring it, so
        the only correct action is to alert while the evidence still exists.
        """
        moment = self.clock() if now is None else now
        cutoff = moment - retention_seconds * 0.8

        def read(conn: sqlite3.Connection) -> list[str]:
            return [
                str(row["lifecycle_id"])
                for row in conn.execute(
                    "SELECT lifecycle_id FROM lead_lifecycles "
                    "WHERE phase = ? AND created_at <= ? AND NOT EXISTS ("
                    "  SELECT 1 FROM lifecycle_facts AS fact "
                    "  WHERE fact.lifecycle_id = lead_lifecycles.lifecycle_id "
                    "  AND fact.fact_type = ?) "
                    "ORDER BY created_at, lifecycle_id",
                    (PHASE_ACTIVE, cutoff, FACT_FIRST_T1_SEND_SUCCESS),
                )
            ]

        return self.runtime.read(read)

    def _claim_human_fact(
        self,
        conn: sqlite3.Connection,
        *,
        contact_key: str,
        event_id: str,
        observed_at: object,
        now: float,
    ) -> bool:
        """Attach the event to this contact's newest active lifecycle it postdates.

        Newest rather than any: a contact may have had an earlier campaign, and
        an event proves a reply to the lifecycle it actually followed.
        """
        if not contact_key or observed_at is None:
            return False
        lifecycle = conn.execute(
            "SELECT lifecycle.lifecycle_id "
            "FROM lead_lifecycles AS lifecycle "
            "JOIN transport_events AS origin "
            "ON origin.event_id = lifecycle.origin_event_id "
            "WHERE lifecycle.contact_key = ? AND lifecycle.phase = ? "
            "AND COALESCE(origin.message_timestamp, origin.received_at) < ? "
            "ORDER BY COALESCE(origin.message_timestamp, origin.received_at) DESC "
            "LIMIT 1",
            (contact_key, PHASE_ACTIVE, observed_at),
        ).fetchone()
        if lifecycle is None:
            return False
        lifecycle_id = str(lifecycle["lifecycle_id"])
        already = conn.execute(
            "SELECT 1 FROM lifecycle_facts WHERE lifecycle_id = ? AND fact_type = ?",
            (lifecycle_id, FACT_FIRST_HUMAN_INBOUND),
        ).fetchone()
        if already is not None:
            return False
        self._record_fact(
            conn,
            lifecycle_id,
            FACT_FIRST_HUMAN_INBOUND,
            evidence_ref=event_id,
            observed_at=float(observed_at),
            now=now,
        )
        return True

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
