"""Bounded orchestration for exact CTWA source-to-Meta attribution."""

from __future__ import annotations

import math
import sqlite3
import time
from typing import Protocol

from .config import BrainSettings
from .meta_ads_mcp import RemoteMetaAdsMcpClient
from .meta_ads_models import (
    ConfirmedMetaAttribution,
    MetaAdsError,
    ObservedCtwaSource,
    RemoteAd,
    RemoteCampaign,
    observed_ctwa_source,
)
from .meta_ads_store import MetaAdsStore
from .runtime_db import RuntimeDatabase


class _MetaClient(Protocol):
    def probe(self, deadline: float | None = None) -> None: ...

    def get_ad(self, source_id: str, deadline: float | None = None) -> object: ...

    def get_campaign(
        self, campaign_id: str, deadline: float | None = None
    ) -> object: ...

    def invalidate(self) -> None: ...


class _ResolutionFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class MetaAttributionService:
    """Keep remote work out of SQLite transactions and persist fenced results."""

    def __init__(
        self,
        settings: BrainSettings,
        runtime: RuntimeDatabase,
        client: _MetaClient | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._store = MetaAdsStore(settings.meta_ad_account_id)
        self._client_instance = client

    def stage_event(
        self, conn: sqlite3.Connection, *, event_id: str, raw: object, now: float
    ) -> None:
        """Stage an eligible event in the caller's existing write transaction."""
        if not self._settings.meta_ads_mcp_enabled:
            return
        observed = observed_ctwa_source(raw)
        if observed is not None:
            self._store.stage_event(conn, event_id, observed, now)

    def resolve_source(
        self, source_id: str, now: float, budget_seconds: float | None = None
    ) -> bool:
        if not self._settings.meta_ads_mcp_enabled:
            return False
        try:
            ObservedCtwaSource(source_id, None)
            budget = self._budget(budget_seconds)
        except (TypeError, ValueError):
            return False
        if self._auth_circuit_open(now):
            return False

        lease_token = self._runtime.write(
            lambda conn: self._store.claim_source_job(
                conn, source_id, now, self._lease_seconds(budget)
            )
        )
        if lease_token is None:
            return False

        probe_revision = self._runtime.read(self._store.auth_state_revision)
        deadline = time.monotonic() + budget
        try:
            client = self._client()
            client.probe(deadline)
            self._runtime.write(
                lambda conn: self._store.close_auth_circuit(conn, now, probe_revision)
            )
            ad = client.get_ad(source_id, deadline)
            confirmed = self._confirmed(source_id, ad, client, deadline)
        except MetaAdsError as error:
            if error.code == "meta_auth_unavailable":
                self._invalidate_client()
            self._fail(source_id, error, now, str(lease_token))
            return False
        except _ResolutionFailure as error:
            self._fail(source_id, MetaAdsError(error.code), now, str(lease_token))
            return False

        return bool(
            self._runtime.write(
                lambda conn: self._store.complete_source(
                    conn, source_id, confirmed, now, str(lease_token)
                )
            )
        )

    def resolve_pending_for_contact(
        self, event_ids: list[str], now: float, budget_seconds: float
    ) -> int:
        if not self._settings.meta_ads_mcp_enabled:
            return 0
        try:
            budget = self._budget(budget_seconds)
        except (TypeError, ValueError):
            return 0
        deadline = time.monotonic() + budget
        resolved = 0
        seen: set[str] = set()
        for event_id in event_ids:
            if not isinstance(event_id, str) or not event_id:
                continue
            source_id = self._runtime.read(
                lambda conn, event_id=event_id: self._pending_source_for_event(
                    conn, event_id
                )
            )
            if source_id is None or source_id in seen:
                continue
            seen.add(source_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self.resolve_source(source_id, now, remaining):
                resolved += 1
            break
        return resolved

    def run_due_jobs(self, now: float, limit: int = 20) -> int:
        if not self._settings.meta_ads_mcp_enabled:
            return 0
        try:
            source_ids = self._runtime.read(
                lambda conn: self._store.due_source_ids(conn, now, limit)
            )
        except (sqlite3.Error, ValueError):
            return 0
        resolved = 0
        for source_id in source_ids:
            if self.resolve_source(source_id, now):
                resolved += 1
        return resolved

    def probe(self, now: float) -> str:
        if not self._settings.meta_ads_mcp_enabled:
            return "disabled"
        probe_revision = self._runtime.read(self._store.auth_state_revision)
        try:
            self._client().probe(time.monotonic() + self._budget(None))
        except MetaAdsError as error:
            if error.code == "meta_auth_unavailable":
                self._invalidate_client()
                retry_after = error.retry_after_seconds or 0.0
                self._runtime.write(
                    lambda conn: self._store.open_auth_circuit(conn, now, retry_after)
                )
            return "degraded"
        self._runtime.write(
            lambda conn: self._store.close_auth_circuit(conn, now, probe_revision)
        )
        return "ready"

    def tick(self, now: float) -> int:
        return self.run_due_jobs(now)

    def health(self, now: float) -> str:
        if not self._settings.meta_ads_mcp_enabled:
            return "disabled"
        return "degraded" if self._auth_circuit_open(now) else "ready"

    def _client(self) -> _MetaClient:
        if self._client_instance is None:
            if not self._settings.meta_ads_mcp_api_key:
                raise MetaAdsError("meta_auth_unavailable")
            self._client_instance = RemoteMetaAdsMcpClient(self._settings)
        return self._client_instance

    def _invalidate_client(self) -> None:
        if self._client_instance is not None:
            self._client_instance.invalidate()

    def _confirmed(
        self, source_id: str, ad: object, client: _MetaClient, deadline: float
    ) -> ConfirmedMetaAttribution:
        try:
            ad_id = ad.ad_id  # type: ignore[attr-defined]
            campaign_id = ad.campaign_id  # type: ignore[attr-defined]
            checked_ad = RemoteAd(
                ad_id,
                ad.name,  # type: ignore[attr-defined]
                campaign_id,
                ad.status,  # type: ignore[attr-defined]
                ad.effective_status,  # type: ignore[attr-defined]
            )
            if ad_id != source_id:
                raise _ResolutionFailure("meta_not_found")
            if checked_ad.effective_status != "ACTIVE":
                raise _ResolutionFailure("meta_inactive")
            campaign = client.get_campaign(campaign_id, deadline)
            checked_campaign = RemoteCampaign(
                campaign.campaign_id,  # type: ignore[attr-defined]
                campaign.name,  # type: ignore[attr-defined]
                campaign.status,  # type: ignore[attr-defined]
                campaign.effective_status,  # type: ignore[attr-defined]
            )
            if checked_campaign.campaign_id != campaign_id:
                raise _ResolutionFailure("meta_not_found")
            if checked_campaign.effective_status != "ACTIVE":
                raise _ResolutionFailure("meta_inactive")
            return ConfirmedMetaAttribution(
                checked_ad.ad_id,
                checked_ad.name,
                checked_ad.campaign_id,
                checked_campaign.name,
                checked_ad.status,
                checked_ad.effective_status,
                checked_campaign.status,
                checked_campaign.effective_status,
            )
        except _ResolutionFailure:
            raise
        except (AttributeError, TypeError, ValueError):
            raise _ResolutionFailure("meta_incomplete_result") from None

    def _fail(
        self, source_id: str, error: MetaAdsError, now: float, lease_token: str
    ) -> None:
        def fail(conn: sqlite3.Connection) -> None:
            if error.code == "meta_auth_unavailable":
                self._store.open_auth_circuit(
                    conn, now, error.retry_after_seconds or 0.0
                )
            self._store.fail_source(conn, source_id, error.code, now, lease_token)

        self._runtime.write(fail)

    def _auth_circuit_open(self, now: float) -> bool:
        return self._runtime.read(
            lambda conn: bool(
                (
                    row := conn.execute(
                        "SELECT auth_circuit_until FROM meta_attribution_state "
                        "WHERE account_id = ?",
                        (self._store.account_id,),
                    ).fetchone()
                )
                and float(row["auth_circuit_until"]) > now
            )
        )

    def _pending_source_for_event(
        self, conn: sqlite3.Connection, event_id: str
    ) -> str | None:
        row = conn.execute(
            "SELECT source_id FROM ctwa_meta_attributions "
            "WHERE event_id = ? AND account_id = ? AND status = 'pending'",
            (event_id, self._store.account_id),
        ).fetchone()
        return None if row is None else str(row["source_id"])

    def _budget(self, budget_seconds: float | None) -> float:
        value = (
            self._settings.meta_ads_mcp_timeout_seconds
            if budget_seconds is None
            else budget_seconds
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("budget_seconds must be a positive finite number")
        return float(value)

    @staticmethod
    def _lease_seconds(budget: float) -> float:
        return max(1.0, budget + 1.0)
