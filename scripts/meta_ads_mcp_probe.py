#!/usr/bin/env python3
"""Run a read-only authenticated capability probe for the fixed Meta Ads MCP."""

from __future__ import annotations

import os
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from brain.config import BrainSettings
from brain.meta_ads_mcp import MetaAdsMcpClient
from brain.meta_ads_models import META_ERROR_CODES, MetaAdsError, canonical_account_id


def _failure_code(error: Exception) -> str:
    if isinstance(error, MetaAdsError):
        return error.code
    return "meta_server_unavailable"


def _configured_account(settings: BrainSettings) -> str:
    environment_account = os.environ.get("BRAIN_META_AD_ACCOUNT_ID")
    if environment_account is not None:
        return environment_account
    config_path = os.environ.get("BRAIN_CONFIG")
    if config_path:
        with Path(config_path).open("rb") as handle:
            config = tomllib.load(handle)
        server = config.get("server")
        if not isinstance(server, dict):
            return ""
        account = server.get("meta_ad_account_id", "")
        return account if isinstance(account, str) else ""
    return settings.meta_ad_account_id


def main(
    *,
    client_factory: Callable[[BrainSettings], Any] = MetaAdsMcpClient,
) -> int:
    try:
        settings = BrainSettings.from_env()
        account_id = canonical_account_id(_configured_account(settings))
        if (
            not settings.meta_ads_mcp_access_token
            or settings.meta_ads_mcp_token_expires_at is None
            or account_id != "1598606388477916"
        ):
            raise MetaAdsError("meta_auth_unavailable")
        client = client_factory(replace(settings, meta_ad_account_id=account_id))
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
