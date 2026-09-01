#!/usr/bin/env python3
"""Create, verify and rotate deployable Brain bundles.

A bundle is the only unit that is ever deployed or restored: one reviewed
commit's Brain code, the CEO plugin built from that same tree, and the
`brain.toml` that matches them. Deploying or restoring any one of the three
alone produces a combination nothing was tested as, and the mismatch stays
silent until the next restart.

Three slots exist, and they are symlinks so a rotation is a rename rather than
a copy:

    candidate  what review approved and the next window will install
    active     what the running service was deployed from
    previous   what active was before the last promotion

The identity of a bundle is the **full** commit SHA. A short SHA is a display
convenience, and this is not a display: two abbreviations can collide, and the
whole point of the slot is to name exactly one tree.

Creation refuses a dirty worktree. A bundle built from uncommitted edits
cannot be rebuilt later, which makes it unverifiable by construction and turns
`previous` into a promise the repository cannot keep.

    python scripts/brain_bundle.py create --config /etc/brain/brain.toml
    python scripts/brain_bundle.py verify candidate
    python scripts/brain_bundle.py promote
    python scripts/brain_bundle.py status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_ROOT = Path("/var/lib/brain/runtime/bundles")
PLUGIN_SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes/brain-ceo-bridge"
SLOTS = ("candidate", "active", "previous")
REQUIRED_GATEWAY_TOOLS = {"conversation_context"}


class BundleError(Exception):
    """Anything that must stop a create, verify or rotate."""


def _git(*args: str, repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BundleError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _require_clean_worktree(repo: Path) -> str:
    """Return the full HEAD SHA, refusing anything that cannot be rebuilt."""
    if _git("status", "--porcelain", repo=repo):
        raise BundleError(
            "worktree is dirty; commit or stash before building a bundle. "
            "A bundle built from uncommitted edits cannot be reproduced, so "
            "it cannot be verified or rolled back to."
        )
    head = _git("rev-parse", "HEAD", repo=repo)
    if len(head) != 40:
        raise BundleError(f"expected a full 40-character SHA, got {head!r}")
    return head


def _validate_config(path: Path) -> None:
    """Refuse a config that does not match the post-Amendment-2 contract."""
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BundleError(f"cannot read {path}: {exc}") from exc
    principals = parsed.get("principals") or {}
    gateway = principals.get("default") or {}
    tools = set(gateway.get("tools") or [])
    if tools != REQUIRED_GATEWAY_TOOLS:
        raise BundleError(
            f"{path}: the default principal grants {sorted(tools)}, expected "
            f"{sorted(REQUIRED_GATEWAY_TOOLS)}. Prepare the post-Amendment-2 "
            "config before building the bundle; this tool never edits it."
        )
    if "writer" in principals:
        raise BundleError(f"{path}: a writer principal must not exist")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_for(directory: Path) -> dict[str, str]:
    return {
        str(item.relative_to(directory)): _digest(item)
        for item in sorted(directory.rglob("*"))
        if item.is_file() and "__pycache__" not in item.parts
    }


def create(root: Path, repo: Path, config: Path) -> Path:
    head = _require_clean_worktree(repo)
    _validate_config(config)

    target = root / head
    if target.exists():
        shutil.rmtree(target)
    (target / "plugin").mkdir(parents=True)
    root.chmod(0o700)

    for item in sorted(PLUGIN_SOURCE.iterdir()):
        if item.is_file():
            shutil.copy2(item, target / "plugin" / item.name)
    shutil.copy2(config, target / "brain.toml")
    (target / "brain.toml").chmod(0o600)

    manifest = _manifest_for(target)
    (target / "MANIFEST.json").write_text(
        json.dumps({"commit": head, "files": manifest}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _point(root, "candidate", head)
    return target


def _point(root: Path, slot: str, sha: str) -> None:
    link = root / slot
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(sha)


def _resolve(root: Path, ref: str) -> Path:
    path = root / ref
    if not path.exists():
        raise BundleError(f"no bundle at {ref}")
    return path.resolve()


def verify(root: Path, ref: str) -> str:
    bundle = _resolve(root, ref)
    manifest_path = bundle / "MANIFEST.json"
    if not manifest_path.is_file():
        raise BundleError(f"{ref}: MANIFEST.json is missing")
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if recorded.get("commit") != bundle.name:
        raise BundleError(f"{ref}: manifest commit does not match directory name")

    expected = {k: v for k, v in recorded["files"].items() if k != "MANIFEST.json"}
    actual = {k: v for k, v in _manifest_for(bundle).items() if k != "MANIFEST.json"}
    if expected != actual:
        changed = sorted(set(expected) ^ set(actual)) or sorted(
            name for name in expected if expected[name] != actual.get(name)
        )
        raise BundleError(f"{ref}: bundle contents changed since creation: {changed}")
    return recorded["commit"]


def promote(root: Path) -> tuple[str, str | None]:
    """Make candidate active, and the outgoing active previous."""
    candidate = _resolve(root, "candidate")
    verify(root, "candidate")

    outgoing = None
    active_link = root / "active"
    if active_link.is_symlink():
        outgoing = active_link.resolve().name
        if outgoing == candidate.name:
            raise BundleError("candidate is already active; nothing to promote")
        _point(root, "previous", outgoing)
    _point(root, "active", candidate.name)
    return candidate.name, outgoing


def status(root: Path) -> list[tuple[str, str, str]]:
    rows = []
    for slot in SLOTS:
        link = root / slot
        if not link.is_symlink():
            rows.append((slot, "-", "unset"))
            continue
        sha = link.resolve().name
        try:
            verify(root, slot)
            state = "verified"
        except BundleError as exc:
            state = f"INVALID: {exc}"
        rows.append((slot, sha, state))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "verify", "promote", "status"))
    parser.add_argument("ref", nargs="?", default="candidate")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, default=Path("/etc/brain/brain.toml"))
    args = parser.parse_args()

    try:
        args.root.mkdir(parents=True, exist_ok=True)
        if args.action == "create":
            target = create(args.root, args.repo, args.config)
            print(f"created {target}")
            print("candidate ->", target.name)
        elif args.action == "verify":
            print(f"{args.ref} verified: {verify(args.root, args.ref)}")
        elif args.action == "promote":
            active, previous = promote(args.root)
            print(f"active   -> {active}")
            print(f"previous -> {previous or '(none: first release)'}")
        else:
            width = max(len(slot) for slot in SLOTS)
            for slot, sha, state in status(args.root):
                print(f"{slot.ljust(width)}  {sha}  {state}")
    except BundleError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
