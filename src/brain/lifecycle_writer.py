"""The only component that may change a lifecycle status in FamaChat.

Spec sections 13 and 16. No model, no LLM, no judgment: it claims one
precomputed effect, proves the record in front of it is the right lead, and
either applies exactly the authorised transition or refuses.

Write mode requires three independent agreements — Brain's claim says write,
this process's own environment says write, and a recorded atomic-write proof
matches the live schema. Any one of them missing leaves it in dry run, which
is a correct terminal state rather than a degraded one.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .famachat_client import (
    FamaChatAmbiguous,
    FamaChatClient,
    FamaChatUnavailable,
    same_phone,
)
from .lifecycle_api import (
    EFFECT_ALREADY_APPLIED,
    EFFECT_CONFLICT,
    EFFECT_RETRYABLE,
    MODE_WRITE,
    ClaimedEffect,
)
from .lifecycle_models import ALLOWED_TRANSITIONS

logger = logging.getLogger("brain.writer")

REQUIRED_BROKER_ID = 35
REQUIRED_SOURCE = "Facebook Ads"

OUTCOME_WOULD_APPLY = "would_apply"
OUTCOME_APPLIED = "applied"
OUTCOME_ALREADY_APPLIED = EFFECT_ALREADY_APPLIED
OUTCOME_CONFLICT = EFFECT_CONFLICT
OUTCOME_RETRYABLE = EFFECT_RETRYABLE
OUTCOME_IDLE = "idle"

IDLE_POLL_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0


@dataclass(frozen=True)
class WriteOutcome:
    """What the writer did, in terms safe to log: no phone, no name, no text."""

    outcome: str
    effect_id: str | None = None
    reason: str | None = None


class LifecycleWriter:
    def __init__(
        self,
        claims,
        famachat: FamaChatClient,
        *,
        write_enabled: bool = False,
        proof_path: Path | None = None,
        schema_fingerprint: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.claims = claims
        self.famachat = famachat
        self.write_enabled = write_enabled
        self.proof_path = proof_path
        self.schema_fingerprint = schema_fingerprint
        self.clock = clock
        # Health-visible counters. Outcome names carry no lead information.
        self.last_outcome: str | None = None
        self.consecutive_failures = 0

    # ------------------------------------------------------------------

    def may_write(self, claim: ClaimedEffect) -> tuple[bool, str | None]:
        """Three independent agreements, all required, checked in one place."""
        if not self.write_enabled:
            return False, "write_disabled_locally"
        if claim.mode != MODE_WRITE:
            return False, "brain_mode_is_not_write"

        proof = self._load_proof()
        if proof is None:
            return False, "no_conditional_write_proof"
        if proof.get("verdict") != "PASS":
            return False, "conditional_write_proof_did_not_pass"
        if self.schema_fingerprint is None:
            return False, "no_live_schema_fingerprint"
        if proof.get("schema_fingerprint") != self.schema_fingerprint:
            # The proof describes a server that is no longer the one in front
            # of us, so it proves nothing about this request.
            return False, "schema_fingerprint_mismatch"
        return True, None

    def run_once(self) -> WriteOutcome:
        """Claim at most one effect and settle it. Returns what happened."""
        claim = self.claims.claim()
        if claim is None:
            return WriteOutcome(OUTCOME_IDLE)

        try:
            outcome = self._settle(claim)
        except FamaChatUnavailable as exc:
            outcome = WriteOutcome(OUTCOME_RETRYABLE, claim.effect_id, reason=str(exc))

        if outcome.outcome != OUTCOME_WOULD_APPLY:
            self.claims.report(claim.effect_id, claim.lease_token, outcome.outcome)

        self.last_outcome = outcome.outcome
        if outcome.outcome == OUTCOME_RETRYABLE:
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
        # Reasons are bounded codes, never a record or a message.
        logger.info(
            "writer settled effect: outcome=%s reason=%s",
            outcome.outcome,
            outcome.reason,
        )
        return outcome

    # ------------------------------------------------------------------

    def _settle(self, claim: ClaimedEffect) -> WriteOutcome:
        record = self.famachat.get_client(claim.client_id)
        if record is None:
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="client_not_found"
            )

        # Identity before anything else: the rest only matters if this really
        # is the lead Brain decided about.
        if record.client_id != claim.client_id:
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="client_id_mismatch"
            )
        if not same_phone(record.phone, claim.expected_phone_e164):
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="phone_mismatch"
            )
        if record.broker_id != REQUIRED_BROKER_ID:
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="broker_mismatch"
            )
        if record.source != REQUIRED_SOURCE:
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="source_mismatch"
            )

        if record.status == claim.target_status:
            # Someone already did it, possibly this writer before a crash.
            return WriteOutcome(OUTCOME_ALREADY_APPLIED, claim.effect_id)
        if record.status != claim.expected_status:
            # A human or another process moved it. Never walk that back.
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="unexpected_current_status"
            )
        if (claim.expected_status, claim.target_status) not in ALLOWED_TRANSITIONS:
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="unauthorised_transition"
            )

        allowed, refusal = self.may_write(claim)
        if not allowed:
            # Everything above was proven; the only missing piece is permission.
            return WriteOutcome(OUTCOME_WOULD_APPLY, claim.effect_id, reason=refusal)

        return self._apply(claim)

    def _apply(self, claim: ClaimedEffect) -> WriteOutcome:
        """Apply the one proven strategy, then read back before believing it."""
        try:
            result = self.famachat.patch_status_conditional(
                claim.client_id,
                expected_status=claim.expected_status,
                target_status=claim.target_status,
            )
        except FamaChatAmbiguous:
            # The write may have landed. Deciding without reading could report
            # a failure that succeeded, or retry a change already applied.
            return self._resolve_unknown(claim)

        if result.conflict:
            # The server refused because the record moved. That is the
            # protection working, not an error.
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="server_refused_stale_state"
            )
        if not result.applied:
            return WriteOutcome(
                OUTCOME_RETRYABLE, claim.effect_id, reason="write_not_applied"
            )
        return self._confirm(claim, applied=True)

    def _resolve_unknown(self, claim: ClaimedEffect) -> WriteOutcome:
        """Read the record to find out what an ambiguous write actually did."""
        try:
            return self._confirm(claim, applied=False)
        except FamaChatUnavailable:
            # Still unknown. Leaving it retryable is safe: the next attempt
            # re-reads first, and the conditional predicate protects it anyway.
            return WriteOutcome(
                OUTCOME_RETRYABLE, claim.effect_id, reason="outcome_unknown"
            )

    def _confirm(self, claim: ClaimedEffect, *, applied: bool) -> WriteOutcome:
        """Readback is mandatory: a 200 is a claim, the record is the evidence."""
        record = self.famachat.get_client(claim.client_id)
        if record is None:
            return WriteOutcome(
                OUTCOME_CONFLICT, claim.effect_id, reason="client_not_found"
            )
        if record.status == claim.target_status:
            return WriteOutcome(
                OUTCOME_APPLIED if applied else OUTCOME_ALREADY_APPLIED,
                claim.effect_id,
            )
        if record.status == claim.expected_status:
            # The write did not land. Nothing was lost, so it can be retried.
            return WriteOutcome(
                OUTCOME_RETRYABLE, claim.effect_id, reason="readback_unchanged"
            )
        return WriteOutcome(
            OUTCOME_CONFLICT, claim.effect_id, reason="readback_unexpected_status"
        )

    def next_delay(self, outcome: WriteOutcome, consecutive_failures: int) -> float:
        """How long to wait before the next attempt.

        A settled effect is followed immediately, since more may be waiting.
        Repeated failure backs off so an unavailable FamaChat is not hammered,
        capped low enough that recovery is still noticed within a minute.
        """
        if outcome.outcome == OUTCOME_RETRYABLE and consecutive_failures > 0:
            return min(IDLE_POLL_SECONDS * 2**consecutive_failures, MAX_BACKOFF_SECONDS)
        if outcome.outcome == OUTCOME_IDLE:
            return IDLE_POLL_SECONDS
        return 0.0

    def health(self) -> dict[str, object]:
        """Operational state only: no lead, phone, client id or status value."""
        proof = self._load_proof()
        return {
            "status": "ok",
            "mode": MODE_WRITE if self.write_enabled else "dry_run",
            "conditional_write_proof": (
                "pass" if proof and proof.get("verdict") == "PASS" else "absent"
            ),
            "schema_fingerprint_known": self.schema_fingerprint is not None,
            "last_outcome": self.last_outcome,
            "consecutive_failures": self.consecutive_failures,
        }

    def _load_proof(self) -> dict | None:
        if self.proof_path is None:
            return None
        try:
            return json.loads(self.proof_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def main() -> int:
    """Entrypoint for brain-lifecycle-writer.service.

    Deliberately refuses to start rather than running unconfigured: a writer
    that silently does nothing looks identical to one that is working, and the
    difference only shows up as leads whose status never moves.
    """
    import os

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    missing = [
        name
        for name in ("BRAIN_WRITER_TOKEN", "BRAIN_URL", "FAMACHAT_MCP_URL")
        if not os.environ.get(name)
    ]
    if missing:
        logger.error("writer is not configured: missing %s", ", ".join(missing))
        return 1

    write_enabled = os.environ.get("BRAIN_LIFECYCLE_WRITE_ENABLED") == "true"
    logger.info(
        "lifecycle writer starting in %s mode",
        MODE_WRITE if write_enabled else "dry_run",
    )
    logger.error(
        "runtime wiring is not implemented yet; the writer refuses to run "
        "rather than appear healthy while doing nothing"
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
