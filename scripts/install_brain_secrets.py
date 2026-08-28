#!/usr/bin/env python3
"""Generate distinct principal tokens and install root-only Brain configuration.

The raw tokens are sent over stdin to Hermes' own dotenv writer and are never
printed or placed on a process command line. The server stores only SHA-256
digests.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

WORKER_PROFILES = ("porteiro", "cadastro", "reno", "famaagent")
GATEWAY_PRINCIPAL = "default"


def env_defines(path: Path, key: str) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if stripped.partition("=")[0].strip() == key:
            return True
    return False


def atomic_private_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_private_write(path: Path, content: str) -> None:
    atomic_private_write_bytes(path, content.encode("utf-8"))


def snapshot_files(paths: Iterable[Path]) -> dict[Path, bytes | None]:
    snapshots: dict[Path, bytes | None] = {}
    for path in paths:
        if path.is_symlink():
            raise RuntimeError(f"refusing to write through symlink: {path}")
        if path.exists() and not path.is_file():
            raise RuntimeError(f"refusing to overwrite non-file: {path}")
        snapshots[path] = path.read_bytes() if path.exists() else None
    return snapshots


def restore_files(snapshots: dict[Path, bytes | None]) -> None:
    for path, content in snapshots.items():
        if content is None:
            if path.is_symlink() or path.exists():
                path.unlink()
        else:
            atomic_private_write_bytes(path, content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path("/root/.hermes"))
    parser.add_argument(
        "--hermes-python",
        type=Path,
        default=Path("/usr/local/lib/hermes-agent/venv/bin/python"),
    )
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/brain"))
    parser.add_argument(
        "--force", action="store_true", help="rotate existing Brain credentials"
    )
    args = parser.parse_args()

    config_path = args.config_dir / "brain.toml"
    env_path = args.config_dir / "brain.env"
    worker_envs = {
        profile: args.hermes_home / "profiles" / profile / ".env"
        for profile in WORKER_PROFILES
    }
    gateway_env = args.hermes_home / ".env"
    if not args.hermes_python.is_file():
        raise RuntimeError("Hermes Python is unavailable")
    if not args.force and (
        config_path.exists()
        or env_defines(gateway_env, "BRAIN_GATEWAY_TOKEN")
        or any(env_defines(path, "BRAIN_TOKEN") for path in worker_envs.values())
    ):
        raise RuntimeError(
            "Brain credentials already exist; use --force only for rotation"
        )

    tokens = {
        GATEWAY_PRINCIPAL: secrets.token_urlsafe(48),
        **{profile: secrets.token_urlsafe(48) for profile in WORKER_PROFILES},
    }
    if len(set(tokens.values())) != len(tokens):  # defensive collision check
        raise RuntimeError("token generation collision")

    writer = (
        "import sys; "
        "from hermes_cli.config import save_env_value; "
        "save_env_value('BRAIN_TOKEN', sys.stdin.read())"
    )
    for profile in WORKER_PROFILES:
        profile_home = args.hermes_home / "profiles" / profile
        if not profile_home.is_dir():
            raise RuntimeError(f"missing Hermes Profile: {profile}")
    secret_targets = [
        (args.hermes_home, "BRAIN_GATEWAY_TOKEN", tokens[GATEWAY_PRINCIPAL])
    ]
    secret_targets.extend(
        (
            args.hermes_home / "profiles" / profile,
            "BRAIN_TOKEN",
            tokens[profile],
        )
        for profile in WORKER_PROFILES
    )
    mutable_paths = [
        *(secret_home / ".env" for secret_home, _, _ in secret_targets),
        config_path,
        env_path,
    ]
    snapshots = snapshot_files(mutable_paths)
    try:
        for secret_home, secret_name, token in secret_targets:
            environment = dict(os.environ)
            environment["HERMES_HOME"] = str(secret_home)
            scoped_writer = writer.replace("BRAIN_TOKEN", secret_name)
            subprocess.run(
                [str(args.hermes_python), "-c", scoped_writer],
                input=token,
                text=True,
                check=True,
                env=environment,
                stdout=subprocess.DEVNULL,
            )

        digests = {
            principal: hashlib.sha256(token.encode("utf-8")).hexdigest()
            for principal, token in tokens.items()
        }
        cursor_secret = secrets.token_hex(32)
        config = f'''[server]
state_db = "/root/.hermes/state.db"
kanban_db = "/root/.hermes/kanban.db"
whatsapp_session_dir = "/root/.hermes/platforms/whatsapp/session"
board = "default"
host = "127.0.0.1"
port = 8765
history_budget_chars = 12000
message_max_chars = 2000
busy_retries = 2
busy_timeout_seconds = 1.0
cursor_secret = "{cursor_secret}"

[principals.default]
mode = "gateway"
token_sha256 = "{digests["default"]}"
tools = ["conversation_phone"]

[principals.porteiro]
mode = "worker"
token_sha256 = "{digests["porteiro"]}"
tools = ["conversation_phone"]

[principals.cadastro]
mode = "worker"
token_sha256 = "{digests["cadastro"]}"
tools = ["conversation_phone"]

[principals.reno]
mode = "worker"
token_sha256 = "{digests["reno"]}"
tools = ["conversation_recent", "conversation_search"]

[principals.famaagent]
mode = "worker"
token_sha256 = "{digests["famaagent"]}"
tools = ["conversation_recent", "conversation_search"]
'''
        atomic_private_write(config_path, config)
        atomic_private_write(env_path, f"BRAIN_CONFIG={config_path}\n")
    except Exception:
        restore_files(snapshots)
        raise
    print(
        "OK: installed distinct worker/gateway credentials and root-only Brain configuration"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary must not print secret-bearing details
        print(
            f"FAIL: Brain secret installation failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
