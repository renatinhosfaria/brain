"""Bounded, content-free readiness probe for the optional Meta Ads MCP."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from brain.config import BrainSettings
from brain.meta_ads_mcp import RemoteMetaAdsMcpClient
from brain.meta_ads_models import MetaAdsError


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        print("error invalid_arguments")
        return 1
    try:
        settings = BrainSettings.from_env()
    except (OSError, TypeError, ValueError):
        print("error config_invalid")
        return 1
    if not settings.meta_ads_mcp_enabled:
        print("disabled")
        return 0
    client = RemoteMetaAdsMcpClient(settings)
    try:
        client.probe()
    except MetaAdsError as error:
        print(f"error {error.code}")
        return 1
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
        print("error meta_server_unavailable")
        return 1
    finally:
        client.close()
    print(f"ready account={settings.meta_ad_account_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
