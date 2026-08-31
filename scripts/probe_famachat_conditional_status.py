#!/usr/bin/env python3
"""Decide whether FamaChat can protect a status write against a lost update.

Two modes, deliberately separate.

``inspect`` reads the captured schema and never touches FamaChat. It can only
ever say CANDIDATE — that a conditional field is declared — because a schema
declares intent, not behaviour. A server that accepts the field and ignores it
looks identical here.

``prove`` performs the one experiment that settles it, against a client the
operator explicitly designates as disposable: apply a conditional change, then
replay a predicate that is now stale and require the server to refuse. Only
that produces the PASS the write gate looks for (spec section 17).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

NO_ATOMIC_PRECONDITION = "NO_ATOMIC_PRECONDITION"
CANDIDATE = "CANDIDATE"
UNAVAILABLE = "UNAVAILABLE"

PATCH_TOOL = "fc_patch_clientes_by_id"
GET_TOOL = "fc_get_clientes_by_id"

# Field names that would carry a server-side expected-state predicate. A schema
# is a candidate only if it declares one of these explicitly; a free-form body
# proves nothing, since every body accepts every key.
CONDITIONAL_FIELDS = ("expectedStatus", "expected_status", "ifMatch", "version")


def inspect(schema_path: Path) -> dict:
    try:
        captured = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"verdict": UNAVAILABLE, "reason": "captured schema is unreadable"}

    tools = {tool["name"]: tool for tool in captured.get("tools", [])}
    if PATCH_TOOL not in tools or GET_TOOL not in tools:
        return {"verdict": UNAVAILABLE, "reason": "required tools are absent"}

    body = (
        (tools[PATCH_TOOL].get("input_schema") or {})
        .get("properties", {})
        .get("body", {})
    )
    declared = body.get("properties") or {}
    found = [name for name in CONDITIONAL_FIELDS if name in declared]
    if not found:
        return {
            "verdict": NO_ATOMIC_PRECONDITION,
            "reason": "the patch body declares no expected-state field",
            "fingerprint": captured.get("fingerprint"),
        }
    return {
        "verdict": CANDIDATE,
        "strategy": "expected_status_in_body",
        "field": found[0],
        "tool": PATCH_TOOL,
        "fingerprint": captured.get("fingerprint"),
        "note": (
            "A declared field is intent, not behaviour. Only the stale-predicate "
            "experiment can turn this into a proof."
        ),
    }


async def _call(url, headers, tool: str, arguments: dict) -> dict:
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
        result = await session.call_tool(tool, arguments)
        raw = result.content[0].text if result.content else "{}"
        return json.loads(raw)


def prove(client_id: int, schema_path: Path) -> dict:
    """Run the one experiment that separates a declared field from a real gate.

    Two writes against a client the operator designated as disposable: one that
    must be applied, then one replaying a predicate that is no longer true and
    must be refused. The client is deliberately left as the experiment leaves
    it; restoring it would be a third production mutation for no evidence.
    """
    import asyncio
    import importlib.util

    inspection = inspect(schema_path)
    if inspection["verdict"] != CANDIDATE:
        return {"verdict": "FAIL", "reason": "inspection is not a candidate"}
    field = inspection["field"]

    spec = importlib.util.spec_from_file_location(
        "cap", str(Path(__file__).with_name("capture_famachat_writer_schema.py"))
    )
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    url, headers = capture.resolve_endpoint(
        Path("/root/.hermes/profiles/reno/config.yaml"),
        Path("/root/.hermes/profiles/reno/.env"),
    )

    def call(tool: str, arguments: dict) -> dict:
        return asyncio.run(_call(url, headers, tool, arguments))

    steps: list[dict] = []

    def record(label: str, http: object, status: object) -> None:
        steps.append({"step": label, "http": http, "status_after": status})

    initial = call(GET_TOOL, {"id": client_id})
    body = initial.get("body") or {}
    if body.get("brokerId") != 35:
        return {"verdict": "FAIL", "reason": "test client is not on broker 35"}
    if body.get("status") != "Sem Atendimento":
        return {
            "verdict": "FAIL",
            "reason": f"test client must start at Sem Atendimento, found {body.get('status')!r}",
        }
    record("initial_get", initial.get("status"), body.get("status"))

    applied = call(
        PATCH_TOOL,
        {
            "id": client_id,
            "body": {"status": "Não Respondeu", field: "Sem Atendimento"},
        },
    )
    record(
        "conditional_apply",
        applied.get("status"),
        (applied.get("body") or {}).get("status"),
    )
    if applied.get("status") != 200:
        return {
            "verdict": "FAIL",
            "reason": "valid predicate was not applied",
            "steps": steps,
        }

    after = call(GET_TOOL, {"id": client_id})
    current = (after.get("body") or {}).get("status")
    record("readback", after.get("status"), current)
    if current != "Não Respondeu":
        return {"verdict": "FAIL", "reason": "apply did not persist", "steps": steps}

    stale = call(
        PATCH_TOOL,
        {
            "id": client_id,
            "body": {"status": "Em Atendimento", field: "Sem Atendimento"},
        },
    )
    record(
        "stale_predicate",
        stale.get("status"),
        (stale.get("body") or {}).get("currentStatus"),
    )

    final = call(GET_TOOL, {"id": client_id})
    final_status = (final.get("body") or {}).get("status")
    record("final_get", final.get("status"), final_status)

    if stale.get("status") != 409:
        return {
            "verdict": "FAIL",
            "reason": f"stale predicate was not refused; server answered {stale.get('status')}",
            "steps": steps,
        }
    if final_status != "Não Respondeu":
        return {
            "verdict": "FAIL",
            "reason": "stale predicate changed the record despite refusing",
            "steps": steps,
        }

    return {
        "verdict": "PASS",
        "strategy": inspection["strategy"],
        "field": field,
        "tools": {"get": GET_TOOL, "patch": PATCH_TOOL},
        "refusal_http_status": 409,
        "schema_fingerprint": inspection["fingerprint"],
        "steps": steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "prove"))
    parser.add_argument("--test-client-id", type=int)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("tests/fixtures/famachat-writer-tools.json"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.mode == "prove":
        designated = os.environ.get("FAMACHAT_CONDITIONAL_TEST_CLIENT_ID")
        if not designated or args.test_client_id is None:
            print(
                "FAIL: prove requires --test-client-id and "
                "FAMACHAT_CONDITIONAL_TEST_CLIENT_ID",
                file=sys.stderr,
            )
            return 1
        if str(args.test_client_id) != designated.strip():
            # The operator designates the disposable client twice, on purpose:
            # this must never run against a client someone passed by accident.
            print(
                "FAIL: test client id does not match the designated one",
                file=sys.stderr,
            )
            return 1
        result = prove(args.test_client_id, args.schema)
    else:
        result = inspect(args.schema)
    result["inspected_at"] = datetime.now(UTC).isoformat()
    serialized = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        os.chmod(args.output, 0o600)
    print(serialized)
    return 0 if result["verdict"] != UNAVAILABLE else 1


if __name__ == "__main__":
    raise SystemExit(main())
