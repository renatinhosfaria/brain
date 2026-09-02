#!/usr/bin/env python3
"""Install or rotate the root-only Meta Ads MCP credential without echoing it."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

try:
    from scripts.install_brain_secrets import (
        atomic_private_write_bytes,
        restore_files,
        snapshot_files,
    )
except ModuleNotFoundError:  # Executed directly as scripts/install_*.py.
    from install_brain_secrets import (  # type: ignore[no-redef]
        atomic_private_write_bytes,
        restore_files,
        snapshot_files,
    )

_TOKEN_KEY = "BRAIN_META_ADS_MCP_ACCESS_TOKEN"
_EXPIRY_KEY = "BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT"
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _valid_expiry(value: str) -> bool:
    if _RFC3339_UTC_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _read_token() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Meta Ads MCP access token: ").strip()
    return sys.stdin.readline().rstrip("\r\n")


def _defines_key(content: bytes, key: str) -> bool:
    for line in content.decode("utf-8-sig", errors="replace").splitlines():
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        if candidate.partition("=")[0].strip() == key:
            return True
    return False


def _with_credential(content: bytes, token: str, expires_at: str) -> bytes:
    lines = content.decode("utf-8-sig", errors="strict").splitlines()
    retained: list[str] = []
    for line in lines:
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        if candidate.partition("=")[0].strip() not in {_TOKEN_KEY, _EXPIRY_KEY}:
            retained.append(line)
    retained.extend((f"{_TOKEN_KEY}={token}", f"{_EXPIRY_KEY}={expires_at}"))
    return ("\n".join(retained) + "\n").encode("utf-8")


def _reject_symlinked_directory(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise RuntimeError("config directory contains a symlink")
        if current == current.parent:
            return
        current = current.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/brain"))
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args(argv)

    try:
        if not _valid_expiry(args.expires_at):
            raise ValueError("expiry is invalid")
        token = _read_token()
        if not token:
            raise ValueError("token is empty")
        _reject_symlinked_directory(args.config_dir)
        env_path = args.config_dir / "brain.env"
        snapshots = snapshot_files((env_path,))
        original = snapshots[env_path] or b""
        if not args.rotate and (
            _defines_key(original, _TOKEN_KEY) or _defines_key(original, _EXPIRY_KEY)
        ):
            raise RuntimeError("credential exists")
        try:
            atomic_private_write_bytes(
                env_path, _with_credential(original, token, args.expires_at)
            )
        except Exception:
            restore_files(snapshots)
            raise
    except Exception:  # noqa: BLE001 - CLI boundary never exposes credential context
        print("FAIL: Meta Ads MCP credential installation failed", file=sys.stderr)
        return 1
    print("OK: Meta Ads MCP credential installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
