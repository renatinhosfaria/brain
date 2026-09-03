"""Validated value objects at the boundary to the remote Meta Ads MCP."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from re import fullmatch

META_READ_TOOLS = frozenset({
    "meta_list_ad_accounts",
    "meta_get_ad",
    "meta_get_campaign",
})
META_ERROR_CODES = frozenset({
    "meta_timeout", "meta_rate_limited", "meta_server_unavailable",
    "meta_auth_unavailable", "meta_required_tool_unavailable",
    "meta_account_mismatch", "meta_not_found", "meta_invalid_response",
    "meta_incomplete_result", "meta_inactive",
})

_ACCOUNT_ID = "1598606388477916"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or fullmatch(r"[0-9]{1,64}", value) is None:
        raise ValueError(f"{field} must be 1-64 ASCII decimal digits")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} contains invalid Unicode") from error
    if encoded_length > 512:
        raise ValueError(f"{field} exceeds 512 UTF-8 bytes")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError(f"{field} contains a control character")
    return value


def normalize_ad_account_id(value: object) -> str:
    """Normalize the one configured account, accepting prefixed or numeric form."""
    if not isinstance(value, str):
        raise TypeError("ad account ID must be a string")
    numeric = value.removeprefix("act_")
    if numeric != _ACCOUNT_ID or value not in {_ACCOUNT_ID, f"act_{_ACCOUNT_ID}"}:
        raise ValueError("ad account ID does not match the configured account")
    return f"act_{_ACCOUNT_ID}"


@dataclass(frozen=True)
class ObservedCtwaSource:
    source_id: str
    ctwa_clid: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _id(self.source_id, "source_id"))
        if self.ctwa_clid is not None:
            object.__setattr__(self, "ctwa_clid", _text(self.ctwa_clid, "ctwa_clid"))


def observed_ctwa_source(raw: object) -> ObservedCtwaSource | None:
    if not isinstance(raw, Mapping) or raw.get("sourceType") != "ad":
        return None
    try:
        return ObservedCtwaSource(raw.get("sourceId"), raw.get("ctwaClid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RemoteAd:
    ad_id: str
    name: str
    campaign_id: str
    status: str
    effective_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ad_id", _id(self.ad_id, "ad_id"))
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "campaign_id", _id(self.campaign_id, "campaign_id"))
        object.__setattr__(self, "status", _text(self.status, "status"))
        object.__setattr__(self, "effective_status", _text(self.effective_status, "effective_status"))


@dataclass(frozen=True)
class RemoteCampaign:
    campaign_id: str
    name: str
    status: str
    effective_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "campaign_id", _id(self.campaign_id, "campaign_id"))
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "status", _text(self.status, "status"))
        object.__setattr__(self, "effective_status", _text(self.effective_status, "effective_status"))


@dataclass(frozen=True)
class ConfirmedMetaAttribution:
    ad_id: str
    ad_name: str
    campaign_id: str
    campaign_name: str
    ad_status: str
    ad_effective_status: str
    campaign_status: str
    campaign_effective_status: str

    def __post_init__(self) -> None:
        for field in ("ad_id", "campaign_id"):
            object.__setattr__(self, field, _id(getattr(self, field), field))
        for field in ("ad_name", "campaign_name", "ad_status", "ad_effective_status", "campaign_status", "campaign_effective_status"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if self.ad_effective_status != "ACTIVE" or self.campaign_effective_status != "ACTIVE":
            raise ValueError("both effective statuses must be ACTIVE")


class MetaAdsError(Exception):
    code: str
    retry_after_seconds: float | None

    def __init__(self, code: str, retry_after_seconds: float | None = None) -> None:
        if code not in META_ERROR_CODES:
            raise ValueError("unknown Meta Ads error code")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(retry_after_seconds)
            or retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds must be a finite non-negative number")
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)
