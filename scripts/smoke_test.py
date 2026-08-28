#!/usr/bin/env python3
"""Post-Hermes-update smoke checks that do not mutate Hermes state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--health-url", default="http://127.0.0.1:8765/health")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--state-db", type=Path, default=Path("/root/.hermes/state.db"))
    parser.add_argument(
        "--kanban-db", type=Path, default=Path("/root/.hermes/kanban.db")
    )
    parser.add_argument(
        "--hermes-python",
        type=Path,
        default=Path("/usr/local/lib/hermes-agent/venv/bin/python"),
    )
    parser.add_argument(
        "--integration-check",
        type=Path,
        default=Path(__file__).with_name("hermes_integration_check.py"),
    )
    args = parser.parse_args()

    if urlparse(args.health_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "FAIL: smoke test must target a localhost Brain endpoint", file=sys.stderr
        )
        return 1
    if not args.state_db.is_file() or not args.kanban_db.is_file():
        print("FAIL: Hermes databases are unavailable", file=sys.stderr)
        return 1
    if not args.hermes_python.is_file() or not args.integration_check.is_file():
        print("FAIL: Hermes integration checker is unavailable", file=sys.stderr)
        return 1
    try:
        resolved = subprocess.run(
            [str(args.hermes_python), str(args.integration_check)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"FAIL: Hermes resolver check failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    if resolved.returncode != 0:
        detail = (resolved.stderr or resolved.stdout).strip()
        print(
            f"FAIL: {detail or 'Hermes integration is incompatible'}", file=sys.stderr
        )
        return 1
    try:
        with urllib.request.urlopen(args.health_url, timeout=3) as response:
            payload = json.load(response)
            status = response.status
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"FAIL: Brain health request failed ({type(exc).__name__})", file=sys.stderr
        )
        return 1
    expected = {
        "status": "ok",
        "hermes_state_db": "ok",
        "hermes_kanban_db": "ok",
        "whatsapp_identity": "compatible",
        "gateway_bridge": "configured",
        "schema": "compatible",
    }
    if status != 200 or payload != expected:
        print("FAIL: Brain health is incompatible", file=sys.stderr)
        return 1

    def post(message: dict, extra_headers: dict[str, str] | None = None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            args.mcp_url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), json.load(response)

    try:
        status, response_headers, initialize = post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "brain-smoke", "version": "1"},
                },
            }
        )
        session_id = response_headers.get("mcp-session-id")
        if status != 200 or not session_id or "result" not in initialize:
            raise RuntimeError("MCP initialize failed")
        session_headers = {"mcp-session-id": session_id}
        status, _, listed = post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_headers,
        )
        tools = listed.get("result", {}).get("tools", [])
        names = {tool.get("name") for tool in tools}
        forbidden = {
            "phone",
            "chat_id",
            "session_id",
            "session_key",
            "task_id",
            "run_id",
            "profile",
            "database_path",
            "mapping_dir",
            "whatsapp_session_dir",
        }
        if status != 200 or names != {
            "conversation_recent",
            "conversation_search",
            "conversation_phone",
        }:
            raise RuntimeError("MCP tool allowlist failed")
        for tool in tools:
            schema = tool.get("inputSchema", {})
            if forbidden.intersection(schema.get("properties", {})):
                raise RuntimeError("MCP identity argument leaked into tool schema")
            if tool.get("name") == "conversation_phone" and schema != {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }:
                raise RuntimeError("conversation_phone schema is not zero-argument")

        # Unresolved Hermes interpolation must fail before any database read.
        status, _, placeholder = post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "conversation_recent", "arguments": {}},
            },
            {
                **session_headers,
                "Authorization": "Bearer ${BRAIN_TOKEN}",
                "X-Hermes-Task": "${HERMES_KANBAN_TASK}",
                "X-Hermes-Run": "${HERMES_KANBAN_RUN_ID}",
            },
        )
        text = placeholder.get("result", {}).get("content", [{}])[0].get("text")
        if status != 200 or text != "Brain access denied for this execution context.":
            raise RuntimeError("placeholder rejection failed")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(
            f"FAIL: MCP compatibility smoke failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1

    print(
        "OK: Hermes databases, resolver, worker capability, MCP transport and Brain allowlist are compatible"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
