#!/usr/bin/env python3
"""Derive the shadow gate results from what the running system actually shows.

The rollout gate reads a results file. That file must be produced by
observation, never by hand: a gate whose input someone types is a gate that
protects nothing. So every check here reads live health, Brain's runtime
database, Hermes' read-only databases, or the upstream integrity checker.

A check that cannot be decided from evidence reports NOT_PROVEN rather than
guessing, and NOT_PROVEN keeps the gate closed exactly like a failure.

    python scripts/collect_shadow_evidence.py \
        --output /var/lib/brain/runtime/ctwa-shadow-results.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RUNTIME_DB = "file:/var/lib/brain/runtime/brain-runtime.db?mode=ro"
KANBAN_DB = "file:/root/.hermes/kanban.db?mode=ro"
BRAIN_HEALTH = "http://127.0.0.1:8765/health"
OBSERVER_HEALTH = "http://127.0.0.1:8775/health"

PASS = "PASS"
NOT_PROVEN = "NOT_PROVEN"


def _health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def _count(database: str, query: str) -> int:
    try:
        connection = sqlite3.connect(database, uri=True)
        try:
            return int(connection.execute(query).fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return 0


def _verdict(condition: bool) -> str:
    return PASS if condition else NOT_PROVEN


def collect() -> dict[str, str]:
    brain = _health(BRAIN_HEALTH)
    observer = _health(OBSERVER_HEALTH)

    gates: dict[str, str] = {}

    # Both WhatsApp devices connected at once is the whole premise of the
    # observer design, so it is checked live rather than remembered.
    gates["OBSERVER_COEXISTENCE"] = _verdict(
        observer.get("whatsapp") == "connected" and brain.get("status") == "ok"
    )
    gates["HERMES_COMPATIBILITY"] = _verdict(
        brain.get("hermes_compatibility") == "compatible"
    )

    gates["RAW_CTWA_CAPTURE"] = _verdict(
        _count(
            RUNTIME_DB,
            "SELECT COUNT(*) FROM transport_events "
            "WHERE transport_kind = 'ctwa_candidate'",
        )
        > 0
    )
    gates["TURN_CORRELATION_CASES"] = _verdict(
        _count(
            RUNTIME_DB,
            "SELECT COUNT(*) FROM whatsapp_turns "
            "WHERE correlation_status = 'correlated'",
        )
        > 0
    )
    # A correlated turn whose mapping actually points at a CTWA event is what
    # conversation_context can serve; a turn alone is not.
    gates["CONVERSATION_CONTEXT_E2E"] = _verdict(
        _count(
            RUNTIME_DB,
            "SELECT COUNT(*) FROM turn_events AS binding "
            "JOIN transport_events AS event ON event.event_id = binding.event_id "
            "JOIN whatsapp_turns AS turn ON turn.wa_turn_id = binding.wa_turn_id "
            "WHERE turn.correlation_status = 'correlated' "
            "AND event.transport_kind = 'ctwa_candidate'",
        )
        > 0
    )

    # All three stage cards of one lead sharing an origin turn is premise P7,
    # and the reason the lifecycle binding is reachable at all.
    gates["KANBAN_IDEMPOTENCY"] = _verdict(
        _count(
            KANBAN_DB,
            "SELECT COUNT(*) FROM (SELECT idempotency_key FROM tasks "
            "WHERE idempotency_key LIKE 'whatsapp:waturn_%' "
            "GROUP BY substr(idempotency_key, 1, 78) HAVING COUNT(*) >= 3)",
        )
        > 0
    )

    gates["LIFECYCLE_SHADOW"] = _verdict(
        _count(RUNTIME_DB, "SELECT COUNT(*) FROM lead_lifecycles") > 0
    )

    # Cadastro rereading by exact id, and Reno reading history once on a new
    # lead, are prompt contracts. They are only proven by a worker actually
    # calling the tool, which its own message history records.
    gates["CADASTRO_READBACK"] = _verdict(
        _count(
            "file:/root/.hermes/profiles/cadastro/state.db?mode=ro",
            "SELECT COUNT(*) FROM messages "
            "WHERE tool_name = 'mcp__famachat__fc_get_clientes_by_id'",
        )
        > 0
    )
    gates["RENO_FIRST_HISTORY"] = _verdict(
        _count(
            "file:/root/.hermes/profiles/reno/state.db?mode=ro",
            "SELECT COUNT(*) FROM messages "
            "WHERE tool_name = 'mcp__brain__conversation_recent'",
        )
        > 0
    )

    try:
        integrity = subprocess.run(
            [
                sys.executable,
                "scripts/hermes_integrity.py",
                "verify",
                "--repo",
                "/usr/local/lib/hermes-agent",
                "--baseline",
                "/var/lib/brain/runtime/hermes-integrity-baseline.json",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        gates["HERMES_ORIGINAL_INTEGRITY"] = _verdict(integrity.returncode == 0)
    except (OSError, subprocess.SubprocessError):
        gates["HERMES_ORIGINAL_INTEGRITY"] = NOT_PROVEN

    # Deliberately not derivable: a restart in the middle of a lifecycle and a
    # writer dry run against a real claim have to be staged, and claiming them
    # from ambient state would be exactly the dishonesty this file prevents.
    for staged in ("RESTART_RECOVERY", "WRITER_DRY_RUN", "CRASH_AFTER_WRITE_RECOVERY"):
        gates[staged] = NOT_PROVEN

    return gates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    gates = collect()
    artifact = {
        "collected_at": datetime.now(UTC).isoformat(),
        "gates": gates,
        "note": (
            "Derived from live system state. Staged scenarios are NOT_PROVEN "
            "until they are actually run."
        ),
    }
    serialized = json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True)

    width = max(len(name) for name in gates)
    for name in sorted(gates):
        marker = " " if gates[name] == PASS else "!"
        print(f"{marker} {name.ljust(width)}  {gates[name]}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
