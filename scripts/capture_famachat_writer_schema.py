#!/usr/bin/env python3
"""Capture the exact FamaChat schemas the lifecycle writer will depend on.

Read-only by construction: MCP handshake and ``tools/list`` only, never a tool
invocation. The writer must build its request envelope from the server's own
declaration rather than from a guess, because a mutation sent in the wrong
shape either fails loudly or, worse, succeeds against the wrong field.

The captured fingerprint is what the write gate compares against later. If the
live schema changes after the atomic-write proof was recorded, the proof no
longer describes the server in front of us and write mode must refuse to start.

    python scripts/capture_famachat_writer_schema.py \
        --output tests/fixtures/famachat-writer-tools.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REQUIRED_TOOLS = ("fc_get_clientes_by_id", "fc_patch_clientes_by_id")
_SECRET = re.compile(r"(Bearer\s+\S+|Authorization[^,}\s]*)", re.IGNORECASE)


def redact(text: object) -> str:
    return _SECRET.sub("<redacted>", str(text))


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_endpoint(config_path: Path, env_path: Path) -> tuple[str, dict[str, str]]:
    import yaml

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    server = (config.get("mcp_servers") or {}).get("famachat") or {}
    url = server.get("url")
    if not url:
        raise RuntimeError("famachat MCP server has no url")

    environment = {**load_env(env_path), **os.environ}
    headers: dict[str, str] = {}
    for name, raw in (server.get("headers") or {}).items():

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            value = environment.get(key)
            if not value:
                raise RuntimeError(f"secret {key} is not available")
            return value

        headers[name] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, str(raw))
    return url, headers


async def fetch_tools(url: str, headers: dict[str, str]) -> list[dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client,
        streamable_http_client,
    )

    async with (
        create_mcp_http_client(headers=headers) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        return [
            {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "input_schema": tool.input_schema,
            }
            for tool in listed.tools
        ]


def fingerprint(tools: list[dict]) -> str:
    """Stable digest over the exact declarations the writer will rely on."""
    canonical = json.dumps(tools, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/root/.hermes/profiles/reno/config.yaml"),
        help="any profile whose famachat server is reachable; tools/list is global",
    )
    parser.add_argument(
        "--env", type=Path, default=Path("/root/.hermes/profiles/reno/.env")
    )
    args = parser.parse_args()

    try:
        url, headers = resolve_endpoint(args.config, args.env)
        tools = asyncio.run(fetch_tools(url, headers))
    except Exception as exc:  # noqa: BLE001 - CLI boundary, redacted
        print(f"FAIL: {type(exc).__name__}: {redact(exc)}", file=sys.stderr)
        return 1

    by_name = {tool["name"]: tool for tool in tools}
    missing = [name for name in REQUIRED_TOOLS if name not in by_name]
    if missing:
        print(f"FAIL: tools ausentes no manifesto: {missing}", file=sys.stderr)
        return 1

    captured = [by_name[name] for name in REQUIRED_TOOLS]
    artifact = {
        "tools": captured,
        "fingerprint": fingerprint(captured),
        "tool_count": len(tools),
    }
    serialized = json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True)
    if _SECRET.search(serialized):
        print("FAIL: captura contém credencial", file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        print(f"PASS: schema capturado em {args.output}")
        print(f"      fingerprint {artifact['fingerprint'][:16]}…")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
