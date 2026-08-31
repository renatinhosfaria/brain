"""Private claim/result boundary for the lifecycle writer.

Spec sections 13 and 16. Brain decides what state is desired and leases one
effect at a time; the writer is the only component holding a credential that
can change FamaChat. Neither service persists the phone that crosses this
boundary — it exists for one comparison against the live record and is gone.

A claim is not permission to write. ``mode`` says whether this deployment is in
shadow, dry run, or write, and the writer refuses to mutate anything unless its
own environment agrees.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .lifecycle_models import (
    ALLOWED_TRANSITIONS,
    EFFECT_PENDING,
    PHASE_ACTIVE,
)
from .runtime_db import RuntimeDatabase
from .transport_models import RuntimeIds

MODE_SHADOW = "shadow"
MODE_DRY_RUN = "dry_run"
MODE_WRITE = "write"
VALID_MODES = frozenset({MODE_SHADOW, MODE_DRY_RUN, MODE_WRITE})

EFFECT_CLAIMED = "claimed"
EFFECT_APPLIED = "applied"
EFFECT_ALREADY_APPLIED = "already_applied"
EFFECT_CONFLICT = "conflict"
EFFECT_RETRYABLE = "retryable"
EFFECT_PERMANENT_FAILURE = "permanent_failure"

TERMINAL_RESULTS = frozenset(
    {EFFECT_APPLIED, EFFECT_ALREADY_APPLIED, EFFECT_CONFLICT, EFFECT_PERMANENT_FAILURE}
)
VALID_RESULTS = TERMINAL_RESULTS | {EFFECT_RETRYABLE}

DEFAULT_LEASE_SECONDS = 120.0


@dataclass(frozen=True)
class ClaimedEffect:
    effect_id: str
    lease_token: str
    client_id: int
    expected_status: str
    target_status: str
    cause: str
    mode: str
    expected_phone_e164: str


class LifecycleClaimService:
    """Lease one effect to the writer, and record what it reports back."""

    def __init__(
        self,
        runtime: RuntimeDatabase,
        runtime_ids: RuntimeIds,
        *,
        resolve_phone: Callable[[str], str | None],
        mode: str = MODE_SHADOW,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError("lifecycle mode is invalid")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.runtime = runtime
        self.runtime_ids = runtime_ids
        self.resolve_phone = resolve_phone
        self.mode = mode
        self.lease_seconds = float(lease_seconds)
        self.clock = clock

    # ------------------------------------------------------------------

    def claim(self) -> ClaimedEffect | None:
        """Lease the oldest pending effect whose contact can still be proven.

        An effect whose phone cannot be resolved is left pending rather than
        handed over: without it the writer cannot tell whether the FamaChat
        record in front of it is the right person.
        """
        now = self.clock()

        def write(conn: sqlite3.Connection) -> ClaimedEffect | None:
            rows = conn.execute(
                "SELECT effect.effect_id, effect.expected_status, "
                "effect.target_status, effect.cause, lifecycle.client_id, "
                "lifecycle.contact_key "
                "FROM lifecycle_effects AS effect "
                "JOIN lead_lifecycles AS lifecycle "
                "ON lifecycle.lifecycle_id = effect.lifecycle_id "
                "WHERE lifecycle.phase = ? AND ("
                "  effect.state = ? OR (effect.state = ? AND effect.lease_expires_at <= ?)"
                ") ORDER BY effect.created_at, effect.effect_id",
                (PHASE_ACTIVE, EFFECT_PENDING, EFFECT_CLAIMED, now),
            ).fetchall()

            for row in rows:
                transition = (
                    str(row["expected_status"]),
                    str(row["target_status"]),
                )
                if transition not in ALLOWED_TRANSITIONS:
                    # Defence in depth: recompute_effects should never have
                    # created this, and the writer must never see it.
                    continue
                phone = self.resolve_phone(str(row["contact_key"]))
                if not phone:
                    continue

                token = secrets.token_hex(16)
                conn.execute(
                    "UPDATE lifecycle_effects SET state = ?, attempts = attempts + 1, "
                    "lease_token_hmac = ?, lease_expires_at = ?, updated_at = ? "
                    "WHERE effect_id = ?",
                    (
                        EFFECT_CLAIMED,
                        self._lease_hmac(token),
                        now + self.lease_seconds,
                        now,
                        row["effect_id"],
                    ),
                )
                return ClaimedEffect(
                    effect_id=str(row["effect_id"]),
                    lease_token=token,
                    client_id=int(row["client_id"]),
                    expected_status=transition[0],
                    target_status=transition[1],
                    cause=str(row["cause"]),
                    mode=self.mode,
                    expected_phone_e164=phone,
                )
            return None

        return self.runtime.write(write)

    def report(self, effect_id: str, lease_token: str, result: str) -> bool:
        """Record what the writer observed. Returns whether it was accepted."""
        if result not in VALID_RESULTS:
            return False
        now = self.clock()

        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT lifecycle_id, state, lease_token_hmac, lease_expires_at, "
                "target_status FROM lifecycle_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None or str(row["state"]) != EFFECT_CLAIMED:
                return False
            stored = str(row["lease_token_hmac"] or "")
            if not stored or not hmac.compare_digest(
                stored, self._lease_hmac(lease_token)
            ):
                # A stale writer must not be able to settle an effect that was
                # already reclaimed by another.
                return False
            if float(row["lease_expires_at"] or 0.0) <= now:
                return False

            conn.execute(
                "UPDATE lifecycle_effects SET state = ?, lease_token_hmac = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE effect_id = ?",
                (result, now, effect_id),
            )
            if result in {EFFECT_APPLIED, EFFECT_ALREADY_APPLIED}:
                # FamaChat is authoritative, so what the writer read back
                # becomes the new baseline for the next comparison.
                conn.execute(
                    "UPDATE lead_lifecycles SET last_proven_status = ?, "
                    "updated_at = ? WHERE lifecycle_id = ?",
                    (str(row["target_status"]), now, row["lifecycle_id"]),
                )
            return True

        return self.runtime.write(write)

    def blocked_contact_count(self) -> int:
        """Pending effects whose contact cannot currently be proven, for health."""

        def read(conn: sqlite3.Connection) -> list[str]:
            return [
                str(row["contact_key"])
                for row in conn.execute(
                    "SELECT lifecycle.contact_key FROM lifecycle_effects AS effect "
                    "JOIN lead_lifecycles AS lifecycle "
                    "ON lifecycle.lifecycle_id = effect.lifecycle_id "
                    "WHERE effect.state = ? AND lifecycle.phase = ?",
                    (EFFECT_PENDING, PHASE_ACTIVE),
                )
            ]

        return sum(1 for key in self.runtime.read(read) if not self.resolve_phone(key))

    def _lease_hmac(self, token: str) -> str:
        """Lease tokens are stored as digests; the raw token is never at rest."""
        return self.runtime_ids.opaque_hmac(token)
