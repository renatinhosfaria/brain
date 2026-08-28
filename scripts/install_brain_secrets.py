#!/usr/bin/env python3
"""Generate distinct Profile tokens and install root-only Brain configuration.

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
from pathlib import Path

PROFILES = ("reno", "famaagent")


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


def atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
    profile_envs = {
        profile: args.hermes_home / "profiles" / profile / ".env"
        for profile in PROFILES
    }
    if not args.hermes_python.is_file():
        raise RuntimeError("Hermes Python is unavailable")
    if not args.force and (
        config_path.exists()
        or any(env_defines(path, "BRAIN_TOKEN") for path in profile_envs.values())
    ):
        raise RuntimeError(
            "Brain credentials already exist; use --force only for rotation"
        )

    tokens = {profile: secrets.token_urlsafe(48) for profile in PROFILES}
    if tokens["reno"] == tokens["famaagent"]:  # defensive, cryptographically negligible
        raise RuntimeError("token generation collision")

    writer = (
        "import sys; "
        "from hermes_cli.config import save_env_value; "
        "save_env_value('BRAIN_TOKEN', sys.stdin.read())"
    )
    for profile, token in tokens.items():
        profile_home = args.hermes_home / "profiles" / profile
        if not profile_home.is_dir():
            raise RuntimeError(f"missing Hermes Profile: {profile}")
        environment = dict(os.environ)
        environment["HERMES_HOME"] = str(profile_home)
        subprocess.run(
            [str(args.hermes_python), "-c", writer],
            input=token,
            text=True,
            check=True,
            env=environment,
            stdout=subprocess.DEVNULL,
        )

    digests = {
        profile: hashlib.sha256(token.encode("utf-8")).hexdigest()
        for profile, token in tokens.items()
    }
    cursor_secret = secrets.token_hex(32)
    config = f'''[server]
state_db = "/root/.hermes/state.db"
kanban_db = "/root/.hermes/kanban.db"
board = "default"
host = "127.0.0.1"
port = 8765
history_budget_chars = 12000
message_max_chars = 2000
busy_retries = 2
busy_timeout_seconds = 1.0
cursor_secret = "{cursor_secret}"

[profiles.reno]
token_sha256 = "{digests["reno"]}"

[profiles.famaagent]
token_sha256 = "{digests["famaagent"]}"
'''
    atomic_private_write(config_path, config)
    atomic_private_write(env_path, f"BRAIN_CONFIG={config_path}\n")
    print("OK: installed distinct Profile tokens and root-only Brain configuration")
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
