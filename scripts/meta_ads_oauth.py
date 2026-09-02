#!/usr/bin/env python3
"""Operate the local, encrypted OAuth connection for Meta Ads MCP.

This command never accepts an OAuth secret through argv and deliberately emits
only bounded status/error text.  Use it from the Brain host through an SSH
tunnel when completing the browser authorization.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from brain.config import BrainSettings
from brain.meta_ads_mcp import MetaAdsMcpClient
from brain.meta_ads_models import MetaAdsError
from brain.meta_ads_oauth import (
    DEFAULT_KEY_PATH,
    DEFAULT_REDIRECT_URI,
    DEFAULT_STORE_PATH,
    MetaAdsOAuth,
    OAuthCallback,
    OAuthCredentialProvider,
    OAuthError,
)


def _read_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().rstrip("\r\n")


def _oauth(args: argparse.Namespace) -> MetaAdsOAuth:
    return MetaAdsOAuth.from_store(
        store_path=args.store_path,
        key_path=args.key_path,
        redirect_uri=DEFAULT_REDIRECT_URI,
    )


def _create_key(path: Path) -> None:
    try:
        if path.exists():
            return
        MetaAdsOAuth._atomic_private_write(path, os.urandom(32))
    except Exception as exc:
        raise OAuthError("oauth_credentials_unavailable") from exc


def _configure(args: argparse.Namespace) -> int:
    client_id = _read_secret("Meta app client ID: ")
    client_secret = _read_secret("Meta app secret (optional): ") or None
    _create_key(args.key_path)
    oauth = MetaAdsOAuth(
        client_id=client_id,
        client_secret=client_secret,
        store_path=args.store_path,
        key_path=args.key_path,
    )
    oauth.save_client_configuration()
    print("OK: Meta Ads OAuth app configuration stored")
    return 0


def _login(args: argparse.Namespace) -> int:
    oauth = _oauth(args)
    request = oauth.authorization_url()
    print("Open this URL after creating the SSH tunnel:")
    print(request.url)
    code = OAuthCallback(request).serve_once()
    oauth.save_credentials(oauth.exchange_code(code, request))
    print("OK: Meta Ads OAuth authorization stored")
    return 0


def _status(args: argparse.Namespace) -> int:
    if not args.store_path.exists():
        print("Meta Ads OAuth status: missing")
        return 0
    provider = OAuthCredentialProvider(_oauth(args))
    print(f"Meta Ads OAuth status: {provider.status(time.time())}")
    return 0


def _clear(args: argparse.Namespace) -> int:
    # Do not decrypt first: rollback must still work if the envelope is
    # corrupted or its key has been replaced.
    MetaAdsOAuth.clear_store(args.store_path)
    print("OK: Meta Ads OAuth credential cleared")
    return 0


def _probe(args: argparse.Namespace) -> int:
    settings = BrainSettings.from_env()
    provider = OAuthCredentialProvider(_oauth(args))
    client = MetaAdsMcpClient(
        # Probe the code-pinned account directly while keeping the attribution
        # worker disabled until the operator explicitly enables it afterwards.
        replace(
            settings,
            meta_ad_account_id="1598606388477916",
            meta_ads_mcp_auth_mode="oauth",
            meta_ads_mcp_access_token="",
            meta_ads_mcp_token_expires_at=None,
        ),
        credential_provider=provider,
    )
    client.probe()
    print("OK: Meta Ads OAuth read-only MCP probe verified")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--key-path", type=Path, default=DEFAULT_KEY_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("configure", "login", "status", "clear", "probe"):
        commands.add_parser(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return {
            "configure": _configure,
            "login": _login,
            "status": _status,
            "clear": _clear,
            "probe": _probe,
        }[args.command](args)
    except (OAuthError, MetaAdsError, ValueError, OSError):
        print("FAIL: Meta Ads OAuth operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
