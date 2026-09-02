"""Durable, exact CTWA-to-Meta Ads attribution orchestration.

The service deliberately keeps every SQLite write short.  The read-only Meta
request happens after a durable job lease has been claimed and before a second
short transaction stores its outcome.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import BrainSettings
from .meta_ads_mcp import MetaAdsClient
from .meta_ads_models import (
    MetaAdRecord,
    MetaAdsCapabilities,
    MetaAdsError,
    MetaAttributionView,
    canonical_account_id,
    eligible_source,
)
from .meta_ads_store import AUTH_CIRCUIT_SECONDS, MetaAdsStore
from .runtime_db import RuntimeDatabase

_LEASE_SECONDS = 30.0
_EXPIRING_CREDENTIAL_SECONDS = 14 * 86_400.0
_REFRESH_BATCH_SIZE = 100


@dataclass(frozen=True)
class MetaAttributionHealth:
    """Non-sensitive component state for the independent Brain health view."""

    status: str
    credential_status: str

    def __post_init__(self) -> None:
        if self.status not in {"disabled", "ready", "degraded"}:
            raise ValueError("invalid Meta attribution health status")
        if self.credential_status not in {"missing", "valid", "expiring", "expired"}:
            raise ValueError("invalid Meta attribution credential status")


class MetaAttributionService:
    """Stage, resolve, retry, and refresh exact source-ID attribution."""

    def __init__(
        self,
        settings: BrainSettings,
        runtime: RuntimeDatabase,
        store: MetaAdsStore,
        client: MetaAdsClient,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._store = store
        self._client = client
        self._monotonic_clock = monotonic_clock
        self._account_id = (
            canonical_account_id(settings.meta_ad_account_id)
            if settings.meta_attribution_enabled
            else store.account_id
        )
        if settings.meta_attribution_enabled and self._account_id != store.account_id:
            raise ValueError("Meta Ads store account does not match settings")
        self._auth_circuit_until = 0.0
        self._probe_succeeded = False
        self._last_incremental_sync_at: float | None = None
        self._last_full_sync_at: float | None = None
        self._last_retention_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.meta_attribution_enabled

    def stage_event(
        self, conn: sqlite3.Connection, *, event_id: str, raw: object, now: float
    ) -> MetaAttributionView | None:
        """Stage an eligible event in the caller's existing ingest transaction."""
        if not self.enabled:
            return None
        observed = eligible_source(raw)
        if observed is None:
            return None
        return self._store.stage_event(conn, event_id, observed, now=now)

    def resolve_source(self, source_id: str, now: float) -> bool:
        """Claim one durable source job, then perform at most one remote lookup."""
        if not self.enabled or self._durable_auth_circuit_active(now):
            return False
        lease_token = self._runtime.write(
            lambda conn: self._store.claim_job(
                conn, source_id, now, lease_seconds=_LEASE_SECONDS
            )
        )
        if lease_token is None:
            return False
        if self._credential_status(now) in {"missing", "expired"}:
            self._auth_circuit_until = max(
                self._auth_circuit_until, now + AUTH_CIRCUIT_SECONDS
            )
            self._fail_claimed_job(
                source_id,
                now,
                "meta_auth_unavailable",
                lease_token,
                retry_after_seconds=AUTH_CIRCUIT_SECONDS,
            )
            self._refresh_auth_circuit(now)
            return False
        if now < self._auth_circuit_until:
            self._fail_claimed_job(
                source_id,
                now,
                "meta_auth_unavailable",
                lease_token,
                retry_after_seconds=self._auth_circuit_until - now,
            )
            self._refresh_auth_circuit(now)
            return False

        request_started = self._monotonic_clock()
        try:
            record = self._client.get_ad(source_id, now)
            if record is None:
                raise MetaAdsError("meta_not_found")
            self._validate_confirmation(source_id, record)
        except MetaAdsError as exc:
            completed_at = self._completed_at(now, request_started)
            if exc.code == "meta_auth_unavailable":
                self._auth_circuit_until = max(
                    self._auth_circuit_until, completed_at + AUTH_CIRCUIT_SECONDS
                )
            self._fail_claimed_job(
                source_id,
                completed_at,
                exc.code,
                lease_token,
                retry_after_seconds=exc.retry_after_seconds,
            )
            if exc.code == "meta_auth_unavailable":
                self._refresh_auth_circuit(completed_at)
            return False
        except (TypeError, ValueError):
            self._fail_claimed_job(
                source_id,
                self._completed_at(now, request_started),
                "meta_invalid_response",
                lease_token,
            )
            return False

        completed_at = self._completed_at(now, request_started)
        confirmed = self._runtime.write(
            lambda conn: self._store.upsert_record_and_confirm(
                conn,
                record,
                confirmed_at=completed_at,
                lease_token=lease_token,
            )
        )
        return bool(confirmed)

    def resolve_contact_pending(self, event_id: str, now: float) -> bool:
        """Attempt the shared job for a single pending context event."""
        if not self.enabled:
            return False
        view = self._runtime.read(
            lambda conn: self._store.context_for_event(conn, event_id)
        )
        return bool(
            view is not None
            and view.status == "pending"
            and self.resolve_source(view.observed.source_id, now)
        )

    def run_due_jobs(self, now: float, *, limit: int = 100) -> int:
        """Resolve due durable jobs; failures remain pending and do not abort a tick."""
        if not self.enabled:
            return 0
        source_ids = self._runtime.read(
            lambda conn: self._store.due_source_ids(conn, now, limit=limit)
        )
        return sum(self.resolve_source(source_id, now) for source_id in source_ids)

    def probe(self, now: float) -> MetaAdsCapabilities | None:
        """Run the explicit capability probe and close the auth circuit on success."""
        if (
            not self.enabled
            or self._durable_auth_circuit_active(now)
            or self._credential_status(now) in {"missing", "expired"}
        ):
            return None
        try:
            capabilities = self._client.probe()
        except MetaAdsError as exc:
            if exc.code == "meta_auth_unavailable":
                self._open_auth_circuit(now, exc.retry_after_seconds)
            self._probe_succeeded = False
            return None
        self._runtime.write(lambda conn: self._store.close_auth_circuit(conn, now))
        self._probe_succeeded = True
        self._auth_circuit_until = 0.0
        return capabilities

    def refresh_catalog(self, now: float, *, full: bool = False) -> int:
        """Refresh validated readable Ads metadata in bounded write batches."""
        if (
            not self.enabled
            or self._durable_auth_circuit_active(now)
            or self._credential_status(now) in {"missing", "expired"}
            or now < self._auth_circuit_until
        ):
            return 0
        try:
            records = self._client.list_ads(now, full)
            for record in records:
                self._validate_confirmation(record.ad_id, record)
        except MetaAdsError as exc:
            if exc.code == "meta_auth_unavailable":
                self._open_auth_circuit(now, exc.retry_after_seconds)
            return 0
        except (TypeError, ValueError):
            return 0

        confirmed = 0
        for start in range(0, len(records), _REFRESH_BATCH_SIZE):
            batch = records[start : start + _REFRESH_BATCH_SIZE]
            confirmed += self._runtime.write(
                lambda conn, batch=batch: sum(
                    self._store.upsert_record_and_confirm(
                        conn, record, confirmed_at=now
                    )
                    for record in batch
                )
            )
        if full:
            self._last_full_sync_at = now
        else:
            self._last_incremental_sync_at = now
        return confirmed

    def apply_retention(self, now: float) -> int:
        if not self.enabled:
            return 0
        self._last_retention_at = now
        return self._runtime.write(lambda conn: self._store.purge_catalog(conn, now))

    def health(self, now: float) -> MetaAttributionHealth:
        credential_status = self._credential_status(now)
        if not self.enabled:
            return MetaAttributionHealth("disabled", credential_status)
        durable_circuit_active = self._durable_auth_circuit_active(now)
        if (
            credential_status in {"missing", "expired"}
            or durable_circuit_active
            or now < self._auth_circuit_until
            or not self._probe_succeeded
        ):
            return MetaAttributionHealth("degraded", credential_status)
        return MetaAttributionHealth("ready", credential_status)

    def tick(self, now: float) -> int:
        """Run bounded due work and service-owned refresh/retention schedules."""
        if not self.enabled:
            return 0
        if self._durable_auth_circuit_active(now):
            if self._last_retention_at is None or (
                now - self._last_retention_at
                >= self._settings.meta_ads_full_sync_interval_seconds
            ):
                self.apply_retention(now)
            return 0
        if not self._probe_succeeded:
            self.probe(now)
        if self._last_incremental_sync_at is None or (
            now - self._last_incremental_sync_at
            >= self._settings.meta_ads_sync_interval_seconds
        ):
            self.refresh_catalog(now, full=False)
        if self._last_full_sync_at is None or (
            now - self._last_full_sync_at
            >= self._settings.meta_ads_full_sync_interval_seconds
        ):
            self.refresh_catalog(now, full=True)
        if self._last_retention_at is None or (
            now - self._last_retention_at
            >= self._settings.meta_ads_full_sync_interval_seconds
        ):
            self.apply_retention(now)
        return self.run_due_jobs(now)

    def _credential_status(self, now: float) -> str:
        if not self._settings.meta_ads_mcp_access_token:
            return "missing"
        expiry = self._settings.meta_ads_mcp_token_expires_at
        if expiry is not None and expiry <= now:
            return "expired"
        if expiry is not None and expiry - now <= _EXPIRING_CREDENTIAL_SECONDS:
            return "expiring"
        return "valid"

    def _completed_at(self, started_at: float, request_started: float) -> float:
        return started_at + max(0.0, self._monotonic_clock() - request_started)

    def _open_auth_circuit(
        self, now: float, retry_after_seconds: float | None = None
    ) -> None:
        circuit_until = self._runtime.write(
            lambda conn: self._store.defer_auth_circuit(
                conn, now, retry_after_seconds=retry_after_seconds
            )
        )
        self._auth_circuit_until = max(self._auth_circuit_until, circuit_until)

    def _refresh_auth_circuit(self, now: float) -> None:
        self._auth_circuit_until = max(
            self._auth_circuit_until,
            self._runtime.read(lambda conn: self._store.auth_circuit_until(conn, now)),
        )

    def _durable_auth_circuit_active(self, now: float) -> bool:
        circuit_until = self._runtime.read(
            lambda conn: self._store.auth_circuit_until(conn, now)
        )
        if circuit_until <= now:
            return False
        self._auth_circuit_until = max(self._auth_circuit_until, circuit_until)
        return True

    def _fail_claimed_job(
        self,
        source_id: str,
        now: float,
        error_code: str,
        lease_token: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self._runtime.write(
            lambda conn: self._store.fail_job(
                conn,
                source_id,
                now,
                error_code,
                lease_token=lease_token,
                retry_after_seconds=retry_after_seconds,
            )
        )

    def _validate_confirmation(self, source_id: str, record: object) -> None:
        if not isinstance(record, MetaAdRecord):
            raise MetaAdsError("meta_invalid_response")
        if record.ad_id != source_id:
            raise MetaAdsError("meta_invalid_response")
        if canonical_account_id(record.account_id) != self._account_id:
            raise MetaAdsError("meta_account_mismatch")
