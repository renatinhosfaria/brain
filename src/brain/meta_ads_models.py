"""Validated domain values for exact CTWA to Meta Ads attribution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

ALLOWED_ACCOUNT_ID = "1598606388477916"
META_ADS_MCP_URL = "https://mcp.facebook.com/ads"
META_ERROR_CODES = frozenset(
    {
        "meta_timeout",
        "meta_rate_limited",
        "meta_server_unavailable",
        "meta_auth_unavailable",
        "meta_required_tool_unavailable",
        "meta_not_found",
        "meta_incomplete_result",
        "meta_account_mismatch",
        "meta_invalid_response",
    }
)
_ID_RE = re.compile(r"^[0-9]{1,64}$", re.ASCII)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _valid_name(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 512


def _valid_timestamp(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def canonical_account_id(value: object) -> str:
    if value == f"act_{ALLOWED_ACCOUNT_ID}" or value == ALLOWED_ACCOUNT_ID:
        return ALLOWED_ACCOUNT_ID
    raise ValueError("Meta Ads account is not the configured account")


@dataclass(frozen=True)
class ObservedAttribution:
    source_id: str
    ctwa_clid: str | None

    def __post_init__(self) -> None:
        if not _valid_id(self.source_id):
            raise ValueError("source_id must contain 1 to 64 ASCII decimal digits")
        if self.ctwa_clid is not None and not _valid_name(self.ctwa_clid):
            raise ValueError(
                "ctwa_clid must be a non-empty UTF-8 value up to 512 bytes"
            )


def eligible_source(raw: object) -> ObservedAttribution | None:
    if not isinstance(raw, dict) or raw.get("sourceType") != "ad":
        return None
    source_id = raw.get("sourceId")
    if not _valid_id(source_id):
        return None
    clid = raw.get("ctwaClid")
    if clid is not None and not _valid_name(clid):
        clid = None
    return ObservedAttribution(source_id, clid)


@dataclass(frozen=True)
class MetaAdRecord:
    account_id: str
    ad_id: str
    ad_name: str
    ad_status: str | None
    ad_effective_status: str | None
    adset_id: str | None
    adset_name: str | None
    adset_status: str | None
    campaign_id: str
    campaign_name: str
    campaign_status: str | None
    creative_id: str | None
    creative_name: str | None
    metadata_complete: bool
    fetched_at: float

    def __post_init__(self) -> None:
        canonical_account_id(self.account_id)
        for name, value, nullable in (
            ("ad_id", self.ad_id, False),
            ("adset_id", self.adset_id, True),
            ("campaign_id", self.campaign_id, False),
            ("creative_id", self.creative_id, True),
        ):
            if value is not None and not _valid_id(value):
                raise ValueError(f"{name} must contain ASCII decimal digits")
            if value is None and not nullable:
                raise ValueError(f"{name} is required")
        for name, value, nullable in (
            ("ad_name", self.ad_name, False),
            ("adset_name", self.adset_name, True),
            ("campaign_name", self.campaign_name, False),
            ("creative_name", self.creative_name, True),
        ):
            if not _valid_name(value, nullable=nullable):
                raise ValueError(f"{name} is invalid")
        for name, value in (
            ("ad_status", self.ad_status),
            ("ad_effective_status", self.ad_effective_status),
            ("adset_status", self.adset_status),
            ("campaign_status", self.campaign_status),
        ):
            if value is not None and not _valid_name(value):
                raise ValueError(f"{name} is invalid")
        if not isinstance(self.metadata_complete, bool):
            raise TypeError("metadata_complete must be boolean")
        if not _valid_timestamp(self.fetched_at):
            raise ValueError("fetched_at must be finite")


@dataclass(frozen=True)
class MetaAdsCapabilities:
    account_id: str
    required_tools: frozenset[str]
    account_argument: str
    entity_selector_argument: str
    result_array_path: tuple[str, ...]
    exact_id_filter_supported: bool

    def __post_init__(self) -> None:
        canonical_account_id(self.account_id)
        if not self.required_tools or not all(
            _valid_name(tool) for tool in self.required_tools
        ):
            raise ValueError("required_tools is invalid")
        if not _valid_name(self.account_argument) or not _valid_name(
            self.entity_selector_argument
        ):
            raise ValueError("MCP argument names are invalid")
        if not isinstance(self.result_array_path, tuple) or not all(
            _valid_name(part) for part in self.result_array_path
        ):
            raise ValueError("result_array_path is invalid")
        if not isinstance(self.exact_id_filter_supported, bool):
            raise TypeError("exact_id_filter_supported must be boolean")


class MetaAdsError(Exception):
    def __init__(self, code: str, retry_after_seconds: float | None = None):
        if code not in META_ERROR_CODES:
            raise ValueError("unsupported Meta Ads error code")
        if retry_after_seconds is not None and (
            not isinstance(retry_after_seconds, (int, float))
            or isinstance(retry_after_seconds, bool)
            or not math.isfinite(retry_after_seconds)
            or retry_after_seconds < 0
        ):
            raise ValueError("invalid retry delay")
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class MetaAttributionView:
    event_id: str
    observed: ObservedAttribution
    status: str
    record: MetaAdRecord | None
    confirmed_at: float | None
    last_attempt_at: float | None
    last_error_code: str | None
    retry_scheduled: bool

    def __post_init__(self) -> None:
        if not _valid_name(self.event_id):
            raise ValueError("event_id is invalid")
        if self.status not in {"pending", "confirmed"}:
            raise ValueError("attribution status is invalid")
        if self.status == "confirmed" and self.record is None:
            raise ValueError("confirmed attribution requires a record")
        if self.status == "pending" and self.record is not None:
            raise ValueError("pending attribution cannot have a record")
        if not _valid_timestamp(self.confirmed_at, nullable=True):
            raise ValueError("confirmed_at must be finite")
        if not _valid_timestamp(self.last_attempt_at, nullable=True):
            raise ValueError("last_attempt_at must be finite")
        if (
            self.last_error_code is not None
            and self.last_error_code not in META_ERROR_CODES
        ):
            raise ValueError("last_error_code is invalid")
        if not isinstance(self.retry_scheduled, bool):
            raise TypeError("retry_scheduled must be boolean")


def _iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def confirmed_payload(
    record: MetaAdRecord, observed: ObservedAttribution, confirmed_at: float
) -> dict[str, Any]:
    if record.ad_id != observed.source_id:
        raise ValueError("confirmed ad must equal source_id")
    if not _valid_timestamp(confirmed_at):
        raise ValueError("confirmed_at must be finite")
    return {
        "status": "confirmed",
        "account_id": f"act_{record.account_id}",
        "matched_by": "source_id_exact",
        "source_id": observed.source_id,
        "ctwa_clid": observed.ctwa_clid,
        "ad": {
            "id": record.ad_id,
            "name": record.ad_name,
            "status": record.ad_effective_status or record.ad_status,
        },
        "adset": None
        if record.adset_id is None
        else {
            "id": record.adset_id,
            "name": record.adset_name,
            "status": record.adset_status,
        },
        "campaign": {
            "id": record.campaign_id,
            "name": record.campaign_name,
            "status": record.campaign_status,
        },
        "creative": None
        if record.creative_id is None
        else {"id": record.creative_id, "name": record.creative_name},
        "metadata_complete": record.metadata_complete,
        "confirmed_at": _iso(confirmed_at),
        "metadata_fetched_at": _iso(record.fetched_at),
    }


def pending_payload(
    observed: ObservedAttribution,
    last_attempt_at: float | None,
    retry_scheduled: bool,
    last_error_code: str | None,
) -> dict[str, Any]:
    if not isinstance(retry_scheduled, bool) or (
        last_error_code is not None and last_error_code not in META_ERROR_CODES
    ):
        raise ValueError("invalid pending attribution state")
    if not _valid_timestamp(last_attempt_at, nullable=True):
        raise ValueError("last_attempt_at must be finite")
    return {
        "status": "pending",
        "source_id": observed.source_id,
        "ctwa_clid": observed.ctwa_clid,
        "last_attempt_at": None if last_attempt_at is None else _iso(last_attempt_at),
        "retry_scheduled": retry_scheduled,
        "last_error_code": last_error_code,
    }
