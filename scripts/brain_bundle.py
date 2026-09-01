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

Prepare the config outside the repository. A file written under `/root/brain`
dirties the worktree, and `create` then refuses the very sequence the runbook
prescribes.

    python scripts/brain_bundle.py create \
        --config /var/lib/brain/runtime/staging/brain.toml.next
    python scripts/brain_bundle.py verify candidate
    python scripts/brain_bundle.py promote
    python scripts/brain_bundle.py rollback
    python scripts/brain_bundle.py status
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_ROOT = Path("/var/lib/brain/runtime/bundles")
PLUGIN_SUBPATH = Path("integrations/hermes/brain-ceo-bridge")
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


def _materialise(target: Path, repo: Path, config: Path, head: str) -> None:
    plugin_source = repo / PLUGIN_SUBPATH
    if not plugin_source.is_dir():
        raise BundleError(f"plugin source is missing: {plugin_source}")
    (target / "plugin").mkdir(parents=True)
    for item in sorted(plugin_source.iterdir()):
        if item.is_file():
            shutil.copy2(item, target / "plugin" / item.name)
    shutil.copy2(config, target / "brain.toml")
    (target / "brain.toml").chmod(0o600)
    (target / "MANIFEST.json").write_text(
        json.dumps(
            {"commit": head, "files": _manifest_for(target)}, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )


def create(root: Path, repo: Path, config: Path) -> Path:
    """Build the bundle for HEAD, or accept an identical one already built.

    A bundle is named by a commit, so its contents are that commit's, and
    rewriting them would silently change what `active` or `previous` means for
    anyone who verified it earlier. An existing directory is therefore never
    deleted: it is rebuilt beside itself and compared. Different is a refusal
    naming the files that disagree; identical is accepted only after it passes
    `verify`, so the acceptance path asserts the same property every other
    caller relies on.
    """
    head = _require_clean_worktree(repo)
    _validate_config(config)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)

    target = root / head
    staging = root / f".staging-{head}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        _materialise(staging, repo, config, head)
        if target.exists():
            # Compare against what is on disk, not against its own manifest: a
            # tampered bundle must be reported, never quietly overwritten.
            existing = _manifest_for(target)
            rebuilt = _manifest_for(staging)
            if existing != rebuilt:
                differing = sorted(
                    set(existing) ^ set(rebuilt)
                ) or sorted(
                    name for name in rebuilt if existing.get(name) != rebuilt[name]
                )
                raise BundleError(
                    f"{head} already exists and differs in {differing}. A bundle "
                    "is immutable; build a new commit instead of rewriting one."
                )
            # Equality already implies the manifest agrees, but acceptance is
            # where "this bundle is good" is asserted, so it goes through the
            # same check every other caller trusts rather than a private
            # shortcut that could drift from it.
            verify(root, head)
        else:
            os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    with exclusive_lock(root):
        state = read_state(root)
        state["candidate"] = head
        state["revision"] = state["revision"] + 1
        write_state(root, state)
    return target


STATE_FILE = "slots.json"
LOCK_FILE = ".slots.lock"
SHA_LENGTH = 40


@contextlib.contextmanager
def exclusive_lock(root: Path):
    """Serialise every read-modify-write on the state.

    `os.replace` makes a write atomic, which prevents a torn file. It does
    nothing about a lost update: two processes can each read the same state,
    each decide a new one, and the second silently discard the first's
    decision. Every mutating path takes this lock around the whole
    read-decide-write, not just the write.
    """
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / LOCK_FILE).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _validate_state(root: Path, state: dict) -> None:
    """Refuse a state that cannot describe a real deployment."""
    active, previous = state.get("active"), state.get("previous")
    if active is not None and active == previous:
        raise BundleError(
            "active and previous cannot be the same bundle: that would leave "
            "no way back from a rollback"
        )
    for slot in SLOTS:
        sha = state.get(slot)
        if sha is None:
            continue
        if not isinstance(sha, str) or len(sha) != SHA_LENGTH:
            raise BundleError(f"{slot}: expected a full {SHA_LENGTH}-character SHA")
        verify(root, sha)


def read_state(root: Path) -> dict[str, str | None]:
    """The authoritative record of what is deployed.

    The symlinks are a convenience for humans and shell scripts. This file is
    the truth: a tampered or stale link changes nothing the tool believes.
    """
    path = root / STATE_FILE
    empty: dict = {**dict.fromkeys(SLOTS), "revision": 0}
    if not path.is_file():
        return empty
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BundleError(f"{path} is not readable state: {exc}") from exc
    state = {slot: stored.get(slot) for slot in SLOTS}
    state["revision"] = int(stored.get("revision", 0))
    return state


def write_state(root: Path, state: dict[str, str | None]) -> None:
    """Commit the whole state in one rename, then redraw the views.

    `active` and `previous` are one fact, not two. Updating them as separate
    symlinks left a window in which an interruption could leave them
    inconsistent, or equal, with no way to tell which had landed.
    """
    _validate_state(root, state)
    staging = root / f".{STATE_FILE}.tmp"
    staging.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(staging, root / STATE_FILE)
    refresh_views(root)


def refresh_views(root: Path) -> None:
    """Redraw the symlinks from the state. Never the other way round."""
    state = read_state(root)
    for slot in SLOTS:
        sha = state.get(slot)
        link = root / slot
        if sha is None:
            if link.is_symlink() or link.exists():
                link.unlink()
            continue
        _point(root, slot, sha)


def _point(root: Path, slot: str, sha: str) -> None:
    """Repoint a slot atomically.

    Unlink-then-symlink leaves a window in which the slot does not exist, and
    an operator or script reading it in that instant sees no bundle at all.
    A rename over the existing link has no such window.
    """
    staging = root / f".{slot}.swap"
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    staging.symlink_to(sha)
    os.replace(staging, root / slot)


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
    """Record that the candidate is the deployment that passed validation.

    Called last, never first. The state must only ever name a release that was
    actually installed and validated, so promoting before the checks would
    make it describe something nobody proved.
    """
    with exclusive_lock(root):
        state = read_state(root)
        candidate = state.get("candidate")
        if candidate is None:
            raise BundleError("no candidate bundle to promote")
        verify(root, candidate)
        if state.get("active") == candidate:
            raise BundleError("candidate is already active; nothing to promote")

        outgoing = state.get("active")
        write_state(
            root,
            {
                "candidate": candidate,
                "active": candidate,
                "previous": outgoing,
                "revision": state["revision"] + 1,
            },
        )
    return candidate, outgoing


def plan_rollback(root: Path) -> dict:
    """Verify the previous bundle and describe the swap. Changes nothing.

    The returned plan names the exact state it was computed against. Recording
    it later compares that description to what is on disk, so a plan made
    before some other change cannot be applied after it — the machine would
    then be running one bundle while the state named another, with nothing to
    say so.
    """
    state = read_state(root)
    previous = state.get("previous")
    if previous is None:
        raise BundleError(
            "no previous bundle: this architecture has never had an earlier "
            "release, so a failure here is answered by rolling forward or by "
            "an explicitly authorized architectural reversion"
        )
    active = state.get("active")
    if active is None:
        raise BundleError("no active bundle to roll back from")
    verify(root, previous)
    return {
        "revision": state["revision"],
        "expected_active": active,
        "target": previous,
    }


def record_rollback(root: Path, plan: dict) -> tuple[str, str]:
    """Record a rollback that was installed and validated, if nothing moved."""
    for field in ("revision", "expected_active", "target"):
        if field not in plan:
            raise BundleError(f"plan is missing {field}")

    with exclusive_lock(root):
        state = read_state(root)
        observed = (state["revision"], state.get("active"), state.get("previous"))
        promised = (plan["revision"], plan["expected_active"], plan["target"])
        if observed != promised:
            raise BundleError(
                "deployment state changed since the plan was made "
                f"(planned revision {promised[0]} active {promised[1]} previous "
                f"{promised[2]}; found revision {observed[0]} active {observed[1]} "
                f"previous {observed[2]}). The installed bundle and the state "
                "would disagree, so nothing was recorded: re-plan and re-validate."
            )
        verify(root, plan["target"])
        write_state(
            root,
            {
                "candidate": state.get("candidate"),
                "active": plan["target"],
                "previous": plan["expected_active"],
                "revision": state["revision"] + 1,
            },
        )
    return plan["target"], plan["expected_active"]


def status(root: Path) -> list[tuple[str, str, str]]:
    state = read_state(root)
    rows = []
    for slot in SLOTS:
        sha = state.get(slot)
        if sha is None:
            rows.append((slot, "-", "unset"))
            continue
        try:
            verify(root, sha)
            state_text = "verified"
        except BundleError as exc:
            state_text = f"INVALID: {exc}"
        rows.append((slot, sha, state_text))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "create",
            "verify",
            "promote",
            "plan-rollback",
            "record-rollback",
            "status",
        ),
    )
    parser.add_argument("ref", nargs="?", default="candidate")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, default=Path("/etc/brain/brain.toml"))
    parser.add_argument(
        "--out",
        type=Path,
        help="plan-rollback: file to write the plan to, for record-rollback to replay",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="record-rollback: the plan file emitted before installation",
    )
    args = parser.parse_args()

    try:
        args.root.mkdir(parents=True, exist_ok=True)
        if args.action == "create":
            target = create(args.root, args.repo, args.config)
            print(f"created {target}")
            print("candidate ->", target.name)
        elif args.action == "verify":
            print(f"{args.ref} verified: {verify(args.root, args.ref)}")
        elif args.action == "plan-rollback":
            plan = plan_rollback(args.root)
            if args.out is None:
                raise BundleError(
                    "--out is required: the plan must be captured now and "
                    "replayed after validation. Planning again afterwards would "
                    "describe whatever the state had become, which is exactly "
                    "the check this design exists to make."
                )
            args.out.write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"target bundle:    {plan['target']}")
            print(f"currently active: {plan['expected_active']}")
            print(f"state revision:   {plan['revision']}")
            print(f"plan written to:  {args.out}")
            print("install this bundle, restart Brain and the Hermes gateway, run")
            print("every validation gate, then replay this exact plan with")
            print(f"  brain_bundle.py record-rollback --plan {args.out}")
            print("nothing has changed yet")
        elif args.action == "record-rollback":
            if args.plan is None:
                raise BundleError("--plan is required: replay the captured plan")
            restored, displaced = record_rollback(
                args.root, json.loads(args.plan.read_text(encoding="utf-8"))
            )
            print(f"active   -> {restored} (restored)")
            print(f"previous -> {displaced} (rolled back from)")
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
