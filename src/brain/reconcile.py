"""One reconciliation pass over everything the lifecycle depends on.

Spec sections 10.2 and 20. Plugin hooks and ingestion callbacks are latency
optimisations; this pass is the durable path. Anything a hook missed — because
Brain was restarting, because a callback raised, because evidence arrived out
of order — is picked up here from Hermes' own databases and Brain's runtime.

Failures are isolated per lifecycle. One lead whose evidence is malformed must
not stop every other lead from reconciling.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .hermes_evidence import HermesEvidenceReader
from .lifecycle_engine import LifecycleEngine

logger = logging.getLogger("brain.reconcile")

RENO_STAGE = "reno"
CADASTRO_STAGE = "cadastro"


@dataclass
class ReconcileReport:
    """Counts only. No lead, phone, client id or message ever appears here."""

    lifecycles_created: int = 0
    human_facts_repaired: int = 0
    deliveries_proven: int = 0
    effects_recomputed: int = 0
    proof_at_risk: int = 0
    retention: dict[str, int] = field(default_factory=dict)
    failures: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "lifecycles_created": self.lifecycles_created,
            "human_facts_repaired": self.human_facts_repaired,
            "deliveries_proven": self.deliveries_proven,
            "effects_recomputed": self.effects_recomputed,
            "proof_at_risk": self.proof_at_risk,
            "retention": dict(self.retention),
            "failures": self.failures,
        }


class Reconciler:
    def __init__(
        self,
        engine: LifecycleEngine,
        evidence: HermesEvidenceReader,
        *,
        display_name_ttl_hours: float = 24.0,
        transport_retention_days: float = 90.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.engine = engine
        self.evidence = evidence
        self.display_name_ttl_hours = display_name_ttl_hours
        self.transport_retention_days = transport_retention_days
        self.clock = clock

    def reconcile_once(self, now: float | None = None) -> ReconcileReport:
        """Run the full pass in dependency order and report counts.

        Order matters: a lifecycle must exist before its facts can attach, and
        its facts must be settled before an effect can be derived from them.
        """
        moment = self.clock() if now is None else now
        report = ReconcileReport()

        self._bind_new_lifecycles(report)
        self._repair_human_facts(report)
        self._prove_deliveries(report, moment)
        self._recompute_effects(report)
        self._flag_proof_at_risk(report, moment)
        self._apply_retention(report, moment)
        return report

    # ------------------------------------------------------------------

    def _bind_new_lifecycles(self, report: ReconcileReport) -> None:
        watermark = self.engine.kanban_watermark()
        for task in self.evidence.list_bound_tasks(after_run_id=watermark):
            if task.stage != CADASTRO_STAGE or task.current_run_id is None:
                continue
            try:
                run = self.evidence.terminal_run(task.task_id, task.current_run_id)
                if run is None:
                    continue
                result = self.engine.bind_completed_cadastro(task, run)
            except Exception:  # noqa: BLE001 - one lead must not stop the rest
                logger.warning("lifecycle binding failed for one task")
                report.failures += 1
                continue
            if result.status == "created":
                report.lifecycles_created += 1
            elif result.status == "conflict":
                # Needs a human: the same origin cannot belong to two clients.
                logger.warning("lifecycle binding conflict requires review")
                report.failures += 1

    def _repair_human_facts(self, report: ReconcileReport) -> None:
        try:
            report.human_facts_repaired = self.engine.repair_human_inbound_facts()
        except Exception:  # noqa: BLE001
            logger.warning("human inbound repair failed")
            report.failures += 1

    def _prove_deliveries(self, report: ReconcileReport, now: float) -> None:
        for task in self.evidence.list_bound_tasks():
            if task.stage != RENO_STAGE or task.current_run_id is None:
                continue
            try:
                run = self.evidence.terminal_run(task.task_id, task.current_run_id)
                if run is None or not run.response_ready:
                    continue
                lifecycle_id = self.engine.lifecycle_for_turn(task.wa_turn_id)
                if lifecycle_id is None:
                    continue
                session_key = self.engine.session_key_for_turn(task.wa_turn_id)
                if session_key is None:
                    continue
                obligations = self.evidence.delivered_obligations(
                    session_key, since=run.started_at or 0.0
                )
                match = self.engine.prove_first_t1_send(lifecycle_id, run, obligations)
            except Exception:  # noqa: BLE001
                logger.warning("delivery proof failed for one lifecycle")
                report.failures += 1
                continue
            if match.status == "proven":
                report.deliveries_proven += 1

    def _recompute_effects(self, report: ReconcileReport) -> None:
        for lifecycle_id in self.engine.active_lifecycle_ids():
            try:
                if self.engine.recompute_effects(lifecycle_id) is not None:
                    report.effects_recomputed += 1
            except Exception:  # noqa: BLE001
                logger.warning("effect recomputation failed for one lifecycle")
                report.failures += 1

    def _flag_proof_at_risk(self, report: ReconcileReport, now: float) -> None:
        try:
            at_risk = self.engine.lifecycles_with_delivery_proof_at_risk(now=now)
        except Exception:  # noqa: BLE001
            logger.warning("delivery retention scan failed")
            report.failures += 1
            return
        report.proof_at_risk = len(at_risk)
        if at_risk:
            # Evidence expires upstream and cannot be reconstructed afterwards.
            logger.warning("delivery proof at risk for %d lifecycle(s)", len(at_risk))

    def _apply_retention(self, report: ReconcileReport, now: float) -> None:
        try:
            report.retention = self.engine.apply_retention(
                now=now,
                display_name_ttl_hours=self.display_name_ttl_hours,
                transport_retention_days=self.transport_retention_days,
            )
        except Exception:  # noqa: BLE001
            logger.warning("retention pass failed")
            report.failures += 1
