"""Transactional SQLite store for exact CTWA Meta Ads attribution."""

from __future__ import annotations

import math
import sqlite3

from .meta_ads_models import (
    META_ERROR_CODES,
    MetaAdRecord,
    MetaAttributionView,
    ObservedAttribution,
    canonical_account_id,
)

RETRY_DELAYS_SECONDS = (60, 300, 900, 3_600, 21_600, 86_400)
AUTH_CIRCUIT_SECONDS = 3_600
CATALOG_RETENTION_SECONDS = 90 * 86_400
MAX_JITTER_SECONDS = 60.0


def _finite(value: object, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _record_from_row(row: sqlite3.Row) -> MetaAdRecord:
    return MetaAdRecord(
        account_id=str(row["account_id"]),
        ad_id=str(row["ad_id"]),
        ad_name=str(row["ad_name"]),
        ad_status=row["ad_status"],
        ad_effective_status=row["ad_effective_status"],
        adset_id=row["adset_id"],
        adset_name=row["adset_name"],
        adset_status=row["adset_status"],
        campaign_id=str(row["campaign_id"]),
        campaign_name=str(row["campaign_name"]),
        campaign_status=row["campaign_status"],
        creative_id=row["creative_id"],
        creative_name=row["creative_name"],
        metadata_complete=bool(row["catalog_metadata_complete"]),
        fetched_at=float(row["fetched_at"]),
    )


class MetaAdsStore:
    """Store operations performed within a transaction owned by the caller."""

    def __init__(self, account_id: str) -> None:
        self.account_id = canonical_account_id(account_id)

    def stage_event(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        observed: ObservedAttribution,
        *,
        now: float,
    ) -> MetaAttributionView:
        now = _finite(now, "now")
        if not isinstance(observed, ObservedAttribution):
            raise TypeError("observed must be ObservedAttribution")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is required")

        existing = conn.execute(
            "SELECT account_id, source_id, ctwa_clid FROM ctwa_meta_attributions "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["account_id"]) != self.account_id
                or str(existing["source_id"]) != observed.source_id
                or existing["ctwa_clid"] != observed.ctwa_clid
            ):
                raise ValueError("event already has a different Meta attribution")
            view = self.context_for_event(conn, event_id)
            assert view is not None
            return view

        cached = conn.execute(
            "SELECT * FROM meta_ads_catalog WHERE account_id = ? AND ad_id = ?",
            (self.account_id, observed.source_id),
        ).fetchone()
        if cached is not None:
            conn.execute(
                "INSERT INTO ctwa_meta_attributions "
                "(event_id, account_id, source_id, ctwa_clid, status, matched_ad_id, "
                "match_method, metadata_complete, confirmed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'confirmed', ?, 'source_id_exact', ?, ?, ?, ?)",
                (
                    event_id,
                    self.account_id,
                    observed.source_id,
                    observed.ctwa_clid,
                    observed.source_id,
                    cached["metadata_complete"],
                    now,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO ctwa_meta_attributions "
                "(event_id, account_id, source_id, ctwa_clid, status, metadata_complete, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
                (
                    event_id,
                    self.account_id,
                    observed.source_id,
                    observed.ctwa_clid,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO meta_attribution_jobs "
                "(account_id, source_id, attempt_count, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?) "
                "ON CONFLICT(account_id, source_id) DO NOTHING",
                (self.account_id, observed.source_id, now, now, now),
            )
        view = self.context_for_event(conn, event_id)
        assert view is not None
        return view

    def claim_job(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        now: float,
        *,
        lease_seconds: float,
    ) -> bool:
        now = _finite(now, "now")
        lease_seconds = _finite(lease_seconds, "lease_seconds")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        ObservedAttribution(source_id, None)
        claimed = conn.execute(
            "UPDATE meta_attribution_jobs SET lease_until = ?, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND next_attempt_at <= ? "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (
                now + lease_seconds,
                now,
                self.account_id,
                source_id,
                now,
                now,
            ),
        ).rowcount
        if claimed != 1:
            return False
        conn.execute(
            "UPDATE ctwa_meta_attributions SET last_attempt_at = ?, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND status = 'pending'",
            (now, now, self.account_id, source_id),
        )
        return True

    def fail_job(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        now: float,
        error_code: str,
        *,
        retry_after_seconds: float | None = None,
        jitter_seconds: float = 0.0,
    ) -> bool:
        now = _finite(now, "now")
        ObservedAttribution(source_id, None)
        if error_code not in META_ERROR_CODES:
            raise ValueError("error_code is invalid")
        jitter_seconds = _finite(jitter_seconds, "jitter_seconds")
        if abs(jitter_seconds) > MAX_JITTER_SECONDS:
            raise ValueError("jitter_seconds exceeds the bounded range")
        if retry_after_seconds is not None:
            retry_after_seconds = _finite(retry_after_seconds, "retry_after_seconds")
            if retry_after_seconds < 0:
                raise ValueError("retry_after_seconds must not be negative")

        job = conn.execute(
            "SELECT attempt_count FROM meta_attribution_jobs "
            "WHERE account_id = ? AND source_id = ?",
            (self.account_id, source_id),
        ).fetchone()
        if job is None:
            return False
        attempt_count = int(job["attempt_count"])
        delay = RETRY_DELAYS_SECONDS[min(attempt_count, len(RETRY_DELAYS_SECONDS) - 1)]
        if error_code == "meta_auth_unavailable":
            delay = max(delay, AUTH_CIRCUIT_SECONDS)
        if retry_after_seconds is not None:
            delay = max(delay, retry_after_seconds)
        delay = min(86_400.0, max(0.0, delay + jitter_seconds))
        conn.execute(
            "UPDATE meta_attribution_jobs SET attempt_count = ?, next_attempt_at = ?, "
            "lease_until = NULL, last_error_code = ?, updated_at = ? "
            "WHERE account_id = ? AND source_id = ?",
            (
                attempt_count + 1,
                now + delay,
                error_code,
                now,
                self.account_id,
                source_id,
            ),
        )
        conn.execute(
            "UPDATE ctwa_meta_attributions SET last_error_code = ?, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND status = 'pending'",
            (error_code, now, self.account_id, source_id),
        )
        return True

    def upsert_record_and_confirm(
        self,
        conn: sqlite3.Connection,
        record: MetaAdRecord,
        *,
        confirmed_at: float,
    ) -> int:
        if not isinstance(record, MetaAdRecord):
            raise TypeError("record must be MetaAdRecord")
        if canonical_account_id(record.account_id) != self.account_id:
            raise ValueError("record belongs to another Meta Ads account")
        confirmed_at = _finite(confirmed_at, "confirmed_at")
        conn.execute(
            "INSERT INTO meta_ads_catalog "
            "(account_id, ad_id, ad_name, ad_status, ad_effective_status, adset_id, "
            "adset_name, adset_status, campaign_id, campaign_name, campaign_status, "
            "creative_id, creative_name, metadata_complete, fetched_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, ad_id) DO UPDATE SET "
            "ad_name = excluded.ad_name, ad_status = excluded.ad_status, "
            "ad_effective_status = excluded.ad_effective_status, "
            "adset_id = excluded.adset_id, adset_name = excluded.adset_name, "
            "adset_status = excluded.adset_status, campaign_id = excluded.campaign_id, "
            "campaign_name = excluded.campaign_name, campaign_status = excluded.campaign_status, "
            "creative_id = excluded.creative_id, creative_name = excluded.creative_name, "
            "metadata_complete = excluded.metadata_complete, fetched_at = excluded.fetched_at, "
            "last_seen_at = excluded.last_seen_at",
            (
                self.account_id,
                record.ad_id,
                record.ad_name,
                record.ad_status,
                record.ad_effective_status,
                record.adset_id,
                record.adset_name,
                record.adset_status,
                record.campaign_id,
                record.campaign_name,
                record.campaign_status,
                record.creative_id,
                record.creative_name,
                int(record.metadata_complete),
                record.fetched_at,
                record.fetched_at,
            ),
        )
        confirmed = conn.execute(
            "UPDATE ctwa_meta_attributions SET status = 'confirmed', matched_ad_id = ?, "
            "match_method = 'source_id_exact', metadata_complete = ?, confirmed_at = ?, "
            "last_error_code = NULL, updated_at = ? "
            "WHERE account_id = ? AND source_id = ? AND status = 'pending'",
            (
                record.ad_id,
                int(record.metadata_complete),
                confirmed_at,
                confirmed_at,
                self.account_id,
                record.ad_id,
            ),
        ).rowcount
        conn.execute(
            "DELETE FROM meta_attribution_jobs WHERE account_id = ? AND source_id = ?",
            (self.account_id, record.ad_id),
        )
        return int(confirmed)

    def context_for_event(
        self, conn: sqlite3.Connection, event_id: str
    ) -> MetaAttributionView | None:
        row = conn.execute(
            "SELECT attribution.event_id, attribution.source_id, attribution.ctwa_clid, "
            "attribution.status, attribution.confirmed_at, attribution.last_attempt_at, "
            "attribution.last_error_code, catalog.account_id AS account_id, "
            "catalog.ad_id, catalog.ad_name, catalog.ad_status, catalog.ad_effective_status, "
            "catalog.adset_id, catalog.adset_name, catalog.adset_status, catalog.campaign_id, "
            "catalog.campaign_name, catalog.campaign_status, catalog.creative_id, "
            "catalog.creative_name, catalog.metadata_complete AS catalog_metadata_complete, "
            "catalog.fetched_at, jobs.source_id AS scheduled_source_id "
            "FROM ctwa_meta_attributions AS attribution "
            "LEFT JOIN meta_ads_catalog AS catalog ON catalog.account_id = attribution.account_id "
            "AND catalog.ad_id = attribution.matched_ad_id "
            "LEFT JOIN meta_attribution_jobs AS jobs ON jobs.account_id = attribution.account_id "
            "AND jobs.source_id = attribution.source_id "
            "WHERE attribution.event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        observed = ObservedAttribution(str(row["source_id"]), row["ctwa_clid"])
        status = str(row["status"])
        record = _record_from_row(row) if status == "confirmed" else None
        return MetaAttributionView(
            event_id=str(row["event_id"]),
            observed=observed,
            status=status,
            record=record,
            confirmed_at=row["confirmed_at"],
            last_attempt_at=row["last_attempt_at"],
            last_error_code=row["last_error_code"],
            retry_scheduled=row["scheduled_source_id"] is not None,
        )

    def due_source_ids(
        self, conn: sqlite3.Connection, now: float, *, limit: int = 100
    ) -> tuple[str, ...]:
        now = _finite(now, "now")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("limit must be between 1 and 500")
        rows = conn.execute(
            "SELECT source_id FROM meta_attribution_jobs WHERE account_id = ? "
            "AND next_attempt_at <= ? AND (lease_until IS NULL OR lease_until <= ?) "
            "ORDER BY next_attempt_at, source_id LIMIT ?",
            (self.account_id, now, now, limit),
        ).fetchall()
        return tuple(str(row["source_id"]) for row in rows)

    def purge_catalog(
        self,
        conn: sqlite3.Connection,
        now: float,
        *,
        retention_seconds: float = CATALOG_RETENTION_SECONDS,
    ) -> int:
        now = _finite(now, "now")
        retention_seconds = _finite(retention_seconds, "retention_seconds")
        if retention_seconds < CATALOG_RETENTION_SECONDS:
            raise ValueError("catalog retention cannot be shorter than 90 days")
        return int(
            conn.execute(
                "DELETE FROM meta_ads_catalog AS catalog WHERE account_id = ? "
                "AND last_seen_at < ? AND NOT EXISTS ("
                "SELECT 1 FROM ctwa_meta_attributions AS attribution "
                "WHERE attribution.account_id = catalog.account_id "
                "AND attribution.matched_ad_id = catalog.ad_id"
                ")",
                (self.account_id, now - retention_seconds),
            ).rowcount
        )
