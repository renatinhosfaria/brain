#!/usr/bin/env python3
"""Run a read-only authenticated capability probe for the fixed Meta Ads MCP."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import Any

from brain.config import BrainSettings
from brain.meta_ads_mcp import MetaAdsMcpClient
from brain.meta_ads_models import META_ERROR_CODES, MetaAdsError, canonical_account_id


def _failure_code(error: Exception) -> str:
    if isinstance(error, MetaAdsError):
        return error.code
    return "meta_server_unavailable"


def main(
    *,
    client_factory: Callable[[BrainSettings], Any] = MetaAdsMcpClient,
) -> int:
    try:
        settings = BrainSettings.from_env()
        if (
            not settings.meta_ads_mcp_access_token
            or settings.meta_ads_mcp_token_expires_at is None
            or canonical_account_id(settings.meta_ad_account_id) != "1598606388477916"
        ):
            raise MetaAdsError("meta_auth_unavailable")
        client = client_factory(settings)
        client.probe()
        known_ad_id = os.environ.get("BRAIN_META_PROBE_AD_ID")
        if known_ad_id:
            client.get_ad(known_ad_id, time.time())
    except Exception as error:  # noqa: BLE001 - never echo remote exception text
        code = _failure_code(error)
        if code not in META_ERROR_CODES:
            code = "meta_server_unavailable"
        print(f"FAIL: {code}", file=sys.stderr)
        return 1
    print("OK: Meta Ads MCP tools, configured account, and known-ad read verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
