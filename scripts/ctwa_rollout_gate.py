#!/usr/bin/env python3
"""Report whether the CTWA lifecycle may run in shadow, or in write mode.

An observer, never an actor. It reads recorded evidence and prints a verdict:
it does not edit an environment file, start or stop a service, or enable write
mode. Turning writes on stays an explicit human action taken after this exits
zero, which is what keeps the decision auditable (spec section 23, Stage 7).

Absent evidence is a failure, not a pass. A gate that treats "no result" as
"no problem" is worse than no gate, because it reports safety it never checked.

    python scripts/ctwa_rollout_gate.py --mode shadow
    python scripts/ctwa_rollout_gate.py --mode write
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RUNTIME = Path("/var/lib/brain/runtime")

SHADOW_GATES = (
    "OBSERVER_COEXISTENCE",
    "RAW_CTWA_CAPTURE",
    "CONVERSATION_CONTEXT_E2E",
    "TURN_CORRELATION_CASES",
    "KANBAN_IDEMPOTENCY",
    "CADASTRO_READBACK",
    "RENO_FIRST_HISTORY",
    "LIFECYCLE_SHADOW",
    "RESTART_RECOVERY",
    "HERMES_COMPATIBILITY",
    "HERMES_ORIGINAL_INTEGRITY",
)

WRITE_GATES = (
    "FAMACHAT_CONDITIONAL_WRITE",
    "CONDITIONAL_SCHEMA_FINGERPRINT_MATCH",
    "WRITER_DRY_RUN",
    "CRASH_AFTER_WRITE_RECOVERY",
)

PASS = "PASS"


def _load(path: Path) -> dict | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def evaluate(
    mode: str,
    *,
    results_path: Path,
    proof_path: Path,
    schema_path: Path,
) -> tuple[bool, list[tuple[str, str]]]:
    """Return whether the mode is allowed, and every gate with its status."""
    results = _load(results_path) or {}
    recorded = results.get("gates") if isinstance(results.get("gates"), dict) else {}

    verdicts: list[tuple[str, str]] = []
    for gate in SHADOW_GATES:
        verdicts.append((gate, str(recorded.get(gate, "MISSING"))))

    if mode == "write":
        proof = _load(proof_path)
        conditional = PASS if proof and proof.get("verdict") == PASS else "NOT_PROVEN"
        verdicts.append(("FAMACHAT_CONDITIONAL_WRITE", conditional))

        captured = _load(schema_path)
        live = captured.get("fingerprint") if captured else None
        recorded_fingerprint = proof.get("schema_fingerprint") if proof else None
        # A proof describes the server it was taken against. If the live schema
        # has moved since, the proof no longer says anything about today.
        matched = (
            PASS
            if live and recorded_fingerprint and live == recorded_fingerprint
            else "MISMATCH"
        )
        verdicts.append(("CONDITIONAL_SCHEMA_FINGERPRINT_MATCH", matched))

        for gate in ("WRITER_DRY_RUN", "CRASH_AFTER_WRITE_RECOVERY"):
            verdicts.append((gate, str(recorded.get(gate, "MISSING"))))

    allowed = all(status == PASS for _, status in verdicts)
    return allowed, verdicts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("shadow", "write"), default="shadow")
    parser.add_argument(
        "--results", type=Path, default=RUNTIME / "ctwa-shadow-results.json"
    )
    parser.add_argument(
        "--proof", type=Path, default=RUNTIME / "famachat-conditional-write-proof.json"
    )
    parser.add_argument(
        "--schema", type=Path, default=Path("tests/fixtures/famachat-writer-tools.json")
    )
    args = parser.parse_args()

    allowed, verdicts = evaluate(
        args.mode,
        results_path=args.results,
        proof_path=args.proof,
        schema_path=args.schema,
    )
    width = max(len(gate) for gate, _ in verdicts)
    for gate, status in verdicts:
        marker = " " if status == PASS else "!"
        print(f"{marker} {gate.ljust(width)}  {status}")

    print()
    if allowed:
        print(f"PASS: {args.mode} mode is permitted by the recorded evidence")
        if args.mode == "write":
            # Deliberately not done here: enabling is a human act, on purpose.
            print("      enabling writes remains a separate, explicit operator action")
        return 0
    print(f"FAIL: {args.mode} mode is not permitted")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
