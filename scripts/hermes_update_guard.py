#!/usr/bin/env python3
"""Bracket a `hermes update` and say what it moved.

An update is expected to move exactly one thing: the upstream installation at
`/usr/local/lib/hermes-agent`. Everything Fama owns — the CEO plugins, the
Profile configs and prompts, Brain's own config — must come out the other side
byte-identical, and the update restarts the gateway on its own, so a change
there takes effect before anyone looks.

This does not prevent anything. It records what the files were, and afterwards
names what differs, separating the expected movement from the alarming kind.
Run it before, run the update, run it after.

    python scripts/hermes_update_guard.py before --out /var/lib/brain/runtime/staging/pre-update.json
    hermes update
    python scripts/hermes_update_guard.py after --snapshot /var/lib/brain/runtime/staging/pre-update.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path("/root/.hermes")
UPSTREAM = Path("/usr/local/lib/hermes-agent")

# Fama-owned files an update must never touch. Listed rather than globbed so a
# file appearing or vanishing is itself reported.
WATCHED = [
    HERMES_HOME / "SOUL.md",
    HERMES_HOME / "config.yaml",
    HERMES_HOME / "skills/business-operations/fama-ceo-runtime/SKILL.md",
    HERMES_HOME / "ops/hermes-team/verify_team.py",
    HERMES_HOME / "ops/hermes-team/reno-famachat-allowlist.json",
    *[
        HERMES_HOME / "profiles" / profile / name
        for profile in ("porteiro", "cadastro", "reno", "famaagent")
        for name in ("config.yaml", "SOUL.md")
    ],
    *[
        HERMES_HOME / "plugins/brain-ceo-bridge" / name
        for name in ("__init__.py", "tools.py", "schemas.py", "plugin.yaml")
    ],
    *[
        HERMES_HOME / "plugins/fama-whatsapp-human-handover" / name
        for name in ("__init__.py", "plugin.yaml", "README.md")
    ],
    Path("/etc/brain/brain.toml"),
]

# Bundled skills are synced by the update on purpose, and this repository
# versions them deliberately, so they move on a routine update. Reporting that
# as a failure trains the operator to ignore the guard, which costs more than
# the check is worth. Anything outside these prefixes is still a finding.
EXPECTED_DIRTY_MARKER = "/skills/"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "<unavailable>"


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _health(port: int) -> str:
    result = subprocess.run(
        ["curl", "-fsS", "--max-time", "5", f"http://127.0.0.1:{port}/health"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "<unreachable>"


def snapshot() -> dict:
    return {
        "upstream": {
            "head": _git(UPSTREAM, "rev-parse", "HEAD"),
            "dirty": _git(UPSTREAM, "status", "--porcelain"),
        },
        "hermes_home": {
            "head": _git(HERMES_HOME, "rev-parse", "HEAD"),
            "dirty": _git(HERMES_HOME, "status", "--porcelain"),
        },
        "files": {str(path): _digest(path) for path in WATCHED},
        "health": {"brain": _health(8765), "observer": _health(8775)},
    }


def _classify_dirty(porcelain: str) -> tuple[list[str], list[str]]:
    """Split `git status --porcelain` into synced skills and everything else."""
    bundled, ours = [], []
    for line in porcelain.splitlines():
        # Split on whitespace rather than a fixed offset. `_git` strips its
        # output, so the first porcelain line loses the leading space of its
        # two-character status field, and a fixed slice then eats a character
        # of the path — which on 2026-09-01 filed a synced skill as a finding.
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        path = parts[1].strip().strip('"')
        if " -> " in path:  # rename: judge the destination
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if not path:
            continue
        target = bundled if (
            path.startswith("skills/")
            or (path.startswith("profiles/") and EXPECTED_DIRTY_MARKER in path)
        ) else ours
        target.append(line.strip())
    return bundled, ours


def compare(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """Return (expected movement, findings that need a human)."""
    expected: list[str] = []
    findings: list[str] = []

    if before["upstream"]["head"] != after["upstream"]["head"]:
        expected.append(
            f"upstream moved {before['upstream']['head'][:12]} -> "
            f"{after['upstream']['head'][:12]}; the integrity baseline no longer "
            "applies and must be re-established by the runbook procedure"
        )
    else:
        findings.append("upstream HEAD did not move: did the update actually run?")
    if after["upstream"]["dirty"]:
        findings.append("upstream worktree is dirty after the update")

    if before["hermes_home"]["head"] != after["hermes_home"]["head"]:
        findings.append(
            "the operational Hermes repository moved; an update should not "
            "commit there"
        )
    bundled, ours = _classify_dirty(after["hermes_home"]["dirty"])
    if bundled:
        expected.append(
            f"{len(bundled)} bundled skill file(s) synced into the operational "
            "repository; review and commit them as an ordinary change"
        )
    if ours:
        findings.append(
            "the operational repository is dirty outside the bundled skills:\n  "
            + "\n  ".join(ours)
        )

    for path, digest in before["files"].items():
        now = after["files"].get(path)
        if digest == now:
            continue
        if digest is None:
            findings.append(f"{path}: appeared (was absent before)")
        elif now is None:
            findings.append(f"{path}: REMOVED by the update")
        else:
            findings.append(f"{path}: CHANGED by the update")

    for service, value in after["health"].items():
        if value == "<unreachable>":
            findings.append(f"{service} health is unreachable")

    return expected, findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("before", "after"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()

    if args.mode == "before":
        if args.out is None:
            print("FAIL: --out is required", file=sys.stderr)
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        args.out.chmod(0o600)
        print(f"snapshot written to {args.out}")
        print("now run: hermes update")
        return 0

    if args.snapshot is None:
        print("FAIL: --snapshot is required", file=sys.stderr)
        return 1
    try:
        before = json.loads(args.snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read {args.snapshot}: {exc}", file=sys.stderr)
        return 1

    expected, findings = compare(before, snapshot())
    for line in expected:
        print(f"  expected: {line}")
    for line in findings:
        print(f"! {line}")

    print()
    if findings:
        print("FAIL: the update moved something it should not have")
        return 1
    print("OK: only the upstream installation moved")
    print("Next, in order:")
    print("  verify_team.py full")
    print("  scripts/hermes_integration_check.py")
    print("  scripts/smoke_test.py")
    print("  re-baseline integrity by the runbook procedure, last")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
