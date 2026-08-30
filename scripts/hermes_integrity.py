"""Capture and verify read-only integrity evidence for upstream Hermes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

SCHEMA_VERSION = 1
MANIFEST_VERSION = 1
CRITICAL_MANIFEST = (
    "scripts/whatsapp-bridge/bridge.js",
    "plugins/platforms/whatsapp/adapter.py",
    "gateway/delivery_ledger.py",
    "gateway/session.py",
    "gateway/session_context.py",
    "tools/kanban_tools.py",
)
_ROOT_FIELDS = {
    "schema_version",
    "manifest_version",
    "git_head",
    "git_clean",
    "files",
}
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_HEAD_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_MAX_BASELINE_BYTES = 1_048_576


class IntegrityError(RuntimeError):
    """Controlled integrity failure containing only a technical reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _fail(reason: str) -> NoReturn:
    raise IntegrityError(reason)


def _safe_repo(repo: Path | str) -> Path:
    candidate = Path(repo)
    try:
        metadata = candidate.lstat()
    except OSError:
        _fail("REPO_INVALID")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("REPO_SYMLINK")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("REPO_INVALID")
    try:
        return candidate.resolve(strict=True)
    except OSError:
        _fail("REPO_INVALID")


def _git(
    repo: Path,
    arguments: Sequence[str],
    *,
    git_executable: Path | str = "git",
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    try:
        result = subprocess.run(
            [str(git_executable), "-C", str(repo), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError):
        _fail("REPO_INVALID")
    return result.stdout.strip()


def _validate_checkout(repo: Path, *, git_executable: Path | str) -> None:
    if (
        _git(
            repo, ("rev-parse", "--is-inside-work-tree"), git_executable=git_executable
        )
        != "true"
    ):
        _fail("REPO_INVALID")
    top_level = _git(
        repo,
        ("rev-parse", "--show-toplevel"),
        git_executable=git_executable,
    )
    try:
        if Path(top_level).resolve(strict=True) != repo:
            _fail("REPO_INVALID")
    except OSError:
        _fail("REPO_INVALID")


def _head(repo: Path, *, git_executable: Path | str) -> str:
    value = _git(repo, ("rev-parse", "HEAD"), git_executable=git_executable)
    if _HEAD_PATTERN.fullmatch(value) is None:
        _fail("REPO_INVALID")
    return value


def _require_clean(repo: Path, *, git_executable: Path | str) -> None:
    status_output = _git(
        repo,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        git_executable=git_executable,
    )
    if status_output:
        _fail("WORKTREE_DIRTY")


def _critical_file(repo: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        _fail("BASELINE_INVALID")
    candidate = repo.joinpath(*pure.parts)
    if not os.path.lexists(candidate):
        _fail("FILE_MISSING")
    try:
        metadata = candidate.lstat()
    except OSError:
        _fail("FILE_MISSING")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("FILE_SYMLINK")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("FILE_NOT_REGULAR")
    try:
        candidate.resolve(strict=True).relative_to(repo)
    except (OSError, ValueError):
        _fail("FILE_PATH_ESCAPE")
    return candidate


def _sha256_file(repo: Path, relative: str) -> str:
    candidate = _critical_file(repo, relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        _fail("FILE_NOT_REGULAR")
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail("FILE_NOT_REGULAR")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail("HASH_MISMATCH")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _hash_manifest(repo: Path) -> dict[str, dict[str, str]]:
    return {
        relative: {"sha256": _sha256_file(repo, relative)}
        for relative in CRITICAL_MANIFEST
    }


def _safe_output_location(repo: Path, output: Path | str) -> Path:
    target = Path(output).absolute()
    if os.path.lexists(target):
        _fail("BASELINE_EXISTS")
    parent = target.parent
    parent_exists = os.path.lexists(parent)
    if parent_exists:
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail("OUTPUT_INVALID")
        try:
            resolved_parent = parent.resolve(strict=True)
        except OSError:
            _fail("OUTPUT_INVALID")
    else:
        try:
            ancestor_metadata = parent.parent.lstat()
            if stat.S_ISLNK(ancestor_metadata.st_mode) or not stat.S_ISDIR(
                ancestor_metadata.st_mode
            ):
                _fail("OUTPUT_INVALID")
            resolved_parent = parent.parent.resolve(strict=True) / parent.name
        except OSError:
            _fail("OUTPUT_INVALID")
    try:
        resolved_parent.relative_to(repo)
    except ValueError:
        if not parent_exists:
            try:
                parent.mkdir(mode=0o700)
            except OSError:
                _fail("OUTPUT_INVALID")
        return resolved_parent / target.name
    _fail("OUTPUT_IN_REPO")


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_install(output: Path, payload: bytes) -> None:
    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(descriptor)
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError:
            _fail("BASELINE_EXISTS")
        os.unlink(temporary)
        temporary = None
        _sync_directory(output.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read_baseline(path: Path | str) -> dict[str, Any]:
    baseline = Path(path)
    if not os.path.lexists(baseline):
        _fail("BASELINE_INVALID")
    try:
        metadata = baseline.lstat()
    except OSError:
        _fail("BASELINE_INVALID")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail("BASELINE_INVALID")
    if metadata.st_size > _MAX_BASELINE_BYTES:
        _fail("BASELINE_INVALID")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(baseline, flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_BASELINE_BYTES + 1)
    except OSError:
        _fail("BASELINE_INVALID")
    if len(raw) > _MAX_BASELINE_BYTES:
        _fail("BASELINE_INVALID")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("BASELINE_INVALID")
    return _validate_baseline(payload)


def _validate_baseline(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS:
        _fail("BASELINE_INVALID")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("manifest_version") != MANIFEST_VERSION
        or payload.get("git_clean") is not True
        or not isinstance(payload.get("git_head"), str)
        or _HEAD_PATTERN.fullmatch(payload["git_head"]) is None
    ):
        _fail("BASELINE_INVALID")
    files = payload.get("files")
    if not isinstance(files, dict) or set(files) != set(CRITICAL_MANIFEST):
        _fail("BASELINE_INVALID")
    for relative in CRITICAL_MANIFEST:
        entry = files.get(relative)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256"}
            or not isinstance(entry.get("sha256"), str)
            or _HASH_PATTERN.fullmatch(entry["sha256"]) is None
        ):
            _fail("BASELINE_INVALID")
    return payload


def capture_baseline(
    repo: Path | str,
    output: Path | str,
    *,
    git_executable: Path | str = "git",
) -> dict[str, Any]:
    """Capture immutable technical evidence from one clean Git checkout."""

    safe_repo = _safe_repo(repo)
    _validate_checkout(safe_repo, git_executable=git_executable)
    if os.path.lexists(output):
        _fail("BASELINE_EXISTS")
    first_head = _head(safe_repo, git_executable=git_executable)
    _require_clean(safe_repo, git_executable=git_executable)
    files = _hash_manifest(safe_repo)
    second_head = _head(safe_repo, git_executable=git_executable)
    if second_head != first_head:
        _fail("HEAD_MISMATCH")
    _require_clean(safe_repo, git_executable=git_executable)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "git_head": first_head,
        "git_clean": True,
        "files": files,
    }
    serialized = (
        json.dumps(payload, sort_keys=True, indent=2, separators=(",", ": ")) + "\n"
    ).encode("utf-8")
    safe_output = _safe_output_location(safe_repo, output)
    _atomic_install(safe_output, serialized)
    return payload


def verify_baseline(
    repo: Path | str,
    baseline: Path | str,
    *,
    git_executable: Path | str = "git",
) -> dict[str, Any]:
    """Verify one checkout against an existing baseline without writing."""

    payload = _read_baseline(baseline)
    safe_repo = _safe_repo(repo)
    _validate_checkout(safe_repo, git_executable=git_executable)
    current_head = _head(safe_repo, git_executable=git_executable)
    if current_head != payload["git_head"]:
        _fail("HEAD_MISMATCH")
    current_files = _hash_manifest(safe_repo)
    for relative in CRITICAL_MANIFEST:
        if current_files[relative]["sha256"] != payload["files"][relative]["sha256"]:
            _fail("HASH_MISMATCH")
    _require_clean(safe_repo, git_executable=git_executable)
    if _head(safe_repo, git_executable=git_executable) != current_head:
        _fail("HEAD_MISMATCH")
    _require_clean(safe_repo, git_executable=git_executable)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--repo", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo", required=True, type=Path)
    verify.add_argument("--baseline", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            capture_baseline(args.repo, args.output)
            print("PASS: BASELINE_CAPTURED")
        else:
            verify_baseline(args.repo, args.baseline)
            print("PASS: BASELINE_VERIFIED")
    except IntegrityError as error:
        print(f"FAIL: {error.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
