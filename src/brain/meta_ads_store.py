"""Durable, caller-transaction-owned storage for CTWA Meta attribution."""

from __future__ import annotations

import math
import secrets
import sqlite3

from .meta_ads_models import (
    META_ERROR_CODES,
    ConfirmedMetaAttribution,
    ObservedCtwaSource,
    normalize_ad_account_id,
)

_RETRY_DELAYS = (60.0, 300.0, 900.0, 3600.0, 21600.0, 86400.0)
_TERMINAL_ERRORS = frozenset(
    {
        "meta_not_found",
        "meta_inactive",
        "meta_account_mismatch",
        "meta_required_tool_unavailable",
    }
)
_AUTH_MINIMUM_DELAY_SECONDS = 60.0
_AUTH_MAXIMUM_DELAY_SECONDS = 86400.0


class MetaAdsStore:
    """Perform only local SQL; callers own the surrounding short transaction."""

    def __init__(self, account_id: str) -> None:
        self.account_id = normalize_ad_account_id(account_id)

    def stage_event(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        observed: ObservedCtwaSource,
        now: float,
    ) -> None:
        if not isinstance(observed, ObservedCtwaSource):
            raise TypeError("observed must be an ObservedCtwaSource")
        now = _time(now)
        existing = conn.execute(
            "SELECT source_id, ctwa_clid FROM ctwa_meta_attributions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["source_id"]) != observed.source_id
                or existing["ctwa_clid"] != observed.ctwa_clid
            ):
                raise ValueError("event replay changes original CTWA source")
            return
        conn.execute(
            "INSERT INTO ctwa_meta_attributions "
            "(event_id, account_id, source_id, ctwa_clid, status, next_attempt_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                event_id,
                self.account_id,
                observed.source_id,
                observed.ctwa_clid,
                now,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO meta_attribution_jobs "
            "(account_id, source_id, next_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, source_id) DO NOTHING",
            (self.account_id, observed.source_id, now, now, now),
        )

    def claim_source_job(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        now: float,
        lease_seconds: float,
    ) -> str | None:
        now = _time(now)
        if not isinstance(lease_seconds, (int, float)) or isinstance(
            lease_seconds, bool
        ):
            raise TypeError("lease_seconds must be a positive finite number")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive finite number")
        token = secrets.token_urlsafe(32)
        lease_until = now + float(lease_seconds)
        claimed = conn.execute(
            "UPDATE meta_attribution_jobs SET lease_until = ?, lease_token = ?, "
            "attempt_count = attempt_count + 1, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND next_attempt_at <= ? "
            "AND (lease_until IS NULL OR lease_until <= ?) "
            "AND NOT EXISTS (SELECT 1 FROM meta_attribution_state "
            "WHERE account_id = ? AND auth_circuit_until > ?)",
            (
                lease_until,
                token,
                now,
                self.account_id,
                source_id,
                now,
                now,
                self.account_id,
                now,
            ),
        ).rowcount
        if claimed != 1:
            return None
        conn.execute(
            "UPDATE ctwa_meta_attributions SET lease_until = ?, lease_token = ?, "
            "last_attempt_at = ?, attempt_count = attempt_count + 1, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND status = 'pending'",
            (lease_until, token, now, now, self.account_id, source_id),
        )
        return token

    def complete_source(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        confirmed: ConfirmedMetaAttribution,
        now: float,
        lease_token: str,
    ) -> int:
        if not isinstance(confirmed, ConfirmedMetaAttribution):
            raise TypeError("confirmed must be a ConfirmedMetaAttribution")
        if confirmed.ad_id != source_id:
            raise ValueError("confirmed ad_id must exactly equal source_id")
        now = _time(now)
        if not _lease_token(lease_token):
            return 0
        owned = conn.execute(
            "DELETE FROM meta_attribution_jobs "
            "WHERE account_id = ? AND source_id = ? AND lease_token = ?",
            (self.account_id, source_id, lease_token),
        ).rowcount
        if owned != 1:
            return 0
        updated = conn.execute(
            "UPDATE ctwa_meta_attributions SET status = 'confirmed', "
            "ad_id = ?, ad_name = ?, campaign_id = ?, campaign_name = ?, "
            "ad_status = ?, ad_effective_status = ?, campaign_status = ?, "
            "campaign_effective_status = ?, match_method = 'source_id_exact', "
            "reason_code = NULL, confirmed_at = ?, next_attempt_at = NULL, "
            "lease_until = NULL, lease_token = NULL, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND status = 'pending'",
            (
                confirmed.ad_id,
                confirmed.ad_name,
                confirmed.campaign_id,
                confirmed.campaign_name,
                confirmed.ad_status,
                confirmed.ad_effective_status,
                confirmed.campaign_status,
                confirmed.campaign_effective_status,
                now,
                now,
                self.account_id,
                source_id,
            ),
        ).rowcount
        return updated or 0

    def fail_source(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        reason_code: str,
        now: float,
        lease_token: str,
    ) -> bool:
        if reason_code not in META_ERROR_CODES:
            raise ValueError("reason_code must be a bounded Meta Ads error code")
        now = _time(now)
        if not _lease_token(lease_token):
            return False
        job = conn.execute(
            "SELECT attempt_count FROM meta_attribution_jobs "
            "WHERE account_id = ? AND source_id = ? AND lease_token = ?",
            (self.account_id, source_id, lease_token),
        ).fetchone()
        if job is None:
            return False
        if reason_code in _TERMINAL_ERRORS:
            conn.execute(
                "UPDATE ctwa_meta_attributions SET status = 'unavailable', reason_code = ?, "
                "next_attempt_at = NULL, lease_until = NULL, lease_token = NULL, "
                "updated_at = ? WHERE account_id = ? AND source_id = ? AND status = 'pending'",
                (reason_code, now, self.account_id, source_id),
            )
            conn.execute(
                "DELETE FROM meta_attribution_jobs WHERE account_id = ? AND source_id = ? "
                "AND lease_token = ?",
                (self.account_id, source_id, lease_token),
            )
            return True
        delay = _retry_delay(int(job["attempt_count"]), reason_code)
        due = now + delay
        conn.execute(
            "UPDATE meta_attribution_jobs SET next_attempt_at = ?, last_error_code = ?, "
            "lease_until = NULL, lease_token = NULL, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND lease_token = ?",
            (due, reason_code, now, self.account_id, source_id, lease_token),
        )
        conn.execute(
            "UPDATE ctwa_meta_attributions SET reason_code = ?, next_attempt_at = ?, "
            "lease_until = NULL, lease_token = NULL, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND status = 'pending'",
            (reason_code, due, now, self.account_id, source_id),
        )
        return True

    def due_source_ids(
        self, conn: sqlite3.Connection, now: float, limit: int
    ) -> list[str]:
        now = _time(now)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        rows = conn.execute(
            "SELECT job.source_id FROM meta_attribution_jobs AS job "
            "LEFT JOIN meta_attribution_state AS state "
            "ON state.account_id = job.account_id "
            "WHERE job.account_id = ? AND job.next_attempt_at <= ? "
            "AND (job.lease_until IS NULL OR job.lease_until <= ?) "
            "AND COALESCE(state.auth_circuit_until, 0) <= ? "
            "ORDER BY job.next_attempt_at, job.source_id LIMIT ?",
            (self.account_id, now, now, now, limit),
        ).fetchall()
        return [str(row["source_id"]) for row in rows]

    def open_auth_circuit(
        self, conn: sqlite3.Connection, now: float, retry_after: float
    ) -> float:
        now = _time(now)
        if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
            raise TypeError("retry_after must be a finite non-negative number")
        if not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError("retry_after must be a finite non-negative number")
        requested_until = now + min(
            max(float(retry_after), _AUTH_MINIMUM_DELAY_SECONDS),
            _AUTH_MAXIMUM_DELAY_SECONDS,
        )
        state = conn.execute(
            "SELECT auth_circuit_until FROM meta_attribution_state WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        until = (
            max(requested_until, float(state["auth_circuit_until"]))
            if state
            else requested_until
        )
        conn.execute(
            "INSERT INTO meta_attribution_state "
            "(account_id, auth_circuit_until, last_probe_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET auth_circuit_until = excluded.auth_circuit_until, "
            "last_probe_at = excluded.last_probe_at, updated_at = excluded.updated_at",
            (self.account_id, until, now, now),
        )
        return until

    def close_auth_circuit(self, conn: sqlite3.Connection, now: float) -> None:
        now = _time(now)
        conn.execute(
            "INSERT INTO meta_attribution_state "
            "(account_id, auth_circuit_until, last_success_at, updated_at) VALUES (?, 0, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET auth_circuit_until = 0, "
            "last_success_at = excluded.last_success_at, updated_at = excluded.updated_at",
            (self.account_id, now, now),
        )

    def context_for_event(
        self, conn: sqlite3.Connection, event_id: str
    ) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT status, reason_code, ad_id, ad_name, campaign_id, campaign_name "
            "FROM ctwa_meta_attributions WHERE event_id = ? AND account_id = ?",
            (event_id, self.account_id),
        ).fetchone()
        if row is None:
            return None
        status = str(row["status"])
        if status == "confirmed":
            return {
                "status": status,
                "ad_id": str(row["ad_id"]),
                "ad_name": str(row["ad_name"]),
                "campaign_id": str(row["campaign_id"]),
                "campaign_name": str(row["campaign_name"]),
            }
        result: dict[str, object] = {"status": status}
        if row["reason_code"] is not None:
            result["reason"] = str(row["reason_code"])
        return result

    def purge_expired(self, conn: sqlite3.Connection, now: float) -> int:
        _time(now)
        removed = conn.execute(
            "DELETE FROM meta_attribution_jobs AS job WHERE job.account_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM ctwa_meta_attributions AS attribution "
            "WHERE attribution.account_id = job.account_id "
            "AND attribution.source_id = job.source_id)",
            (self.account_id,),
        ).rowcount
        return removed or 0


def _retry_delay(attempt_count: int, reason_code: str) -> float:
    if reason_code == "meta_auth_unavailable":
        return _AUTH_MAXIMUM_DELAY_SECONDS
    return _RETRY_DELAYS[min(max(attempt_count, 1), len(_RETRY_DELAYS)) - 1]


def _time(value: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError("timestamp must be finite")
    return float(value)


def _lease_token(value: object) -> bool:
    return isinstance(value, str) and bool(value)
