#!/usr/bin/env python3
"""Verify Brain's Hermes-facing contract with the installed Hermes runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hermes-root", type=Path, default=Path("/usr/local/lib/hermes-agent")
    )
    parser.add_argument(
        "--hermes-python",
        type=Path,
        default=Path("/usr/local/lib/hermes-agent/venv/bin/python"),
    )
    parser.add_argument(
        "--reno-config",
        type=Path,
        default=Path("/root/.hermes/profiles/reno/config.yaml"),
    )
    parser.add_argument(
        "--famaagent-config",
        type=Path,
        default=Path("/root/.hermes/profiles/famaagent/config.yaml"),
    )
    parser.add_argument(
        "--ceo-config", type=Path, default=Path("/root/.hermes/config.yaml")
    )
    parser.add_argument(
        "--server-config", type=Path, default=Path("/etc/brain/brain.toml")
    )
    args = parser.parse_args()

    if not args.hermes_root.is_dir():
        fail("Hermes install directory is missing")
    if (
        args.hermes_python.is_file()
        and Path(sys.executable).resolve() != args.hermes_python.resolve()
    ):
        return subprocess.call(
            [str(args.hermes_python), str(Path(__file__).resolve()), *sys.argv[1:]]
        )
    sys.path.insert(0, str(args.hermes_root))

    import yaml  # type: ignore[import-not-found]
    from agent.secret_scope import load_env_file
    from hermes_cli.tools_config import _get_platform_tools
    from tools.mcp_tool import _interpolate_env_vars

    def load(path: Path) -> dict:
        if not path.is_file():
            fail(f"missing config: {path}")
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(parsed, dict):
            fail(f"invalid config mapping: {path}")
        return parsed

    workers = {
        "reno": load(args.reno_config),
        "famaagent": load(args.famaagent_config),
    }
    ceo = load(args.ceo_config)
    if not args.server_config.is_file():
        fail("Brain server configuration is missing")
    server_config = tomllib.loads(args.server_config.read_text(encoding="utf-8"))
    configured_profiles = server_config.get("profiles") or {}
    token_digests: dict[str, str] = {}

    expected_headers = {
        "Authorization": "Bearer ${BRAIN_TOKEN}",
        "X-Hermes-Task": "${HERMES_KANBAN_TASK}",
        "X-Hermes-Run": "${HERMES_KANBAN_RUN_ID}",
    }
    for profile, config in workers.items():
        token = load_env_file(
            (args.reno_config if profile == "reno" else args.famaagent_config).parent
            / ".env"
        ).get("BRAIN_TOKEN")
        expected_digest = (configured_profiles.get(profile) or {}).get("token_sha256")
        if not token or not isinstance(expected_digest, str):
            fail(f"{profile}: Brain credentials are incomplete")
        actual_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if actual_digest != expected_digest:
            fail(f"{profile}: Profile token does not match the server digest")
        token_digests[profile] = actual_digest

        server = (config.get("mcp_servers") or {}).get("brain")
        if not isinstance(server, dict):
            fail(f"{profile}: mcp_servers.brain missing")
        if server.get("url") != "http://127.0.0.1:8765/mcp":
            fail(f"{profile}: Brain URL is not the localhost production endpoint")
        if server.get("headers") != expected_headers:
            fail(f"{profile}: Brain capability headers changed")
        if set((server.get("tools") or {}).get("include") or []) != {
            "conversation_recent",
            "conversation_search",
        }:
            fail(f"{profile}: Brain tools.include is not the exact V1 allowlist")
        if server.get("resources") is not False or server.get("prompts") is not False:
            fail(f"{profile}: Brain resources/prompts must be disabled")

        cli = _get_platform_tools(config, "cli", include_default_mcp_servers=True)
        telegram = _get_platform_tools(
            config, "telegram", include_default_mcp_servers=True
        )
        whatsapp = _get_platform_tools(
            config, "whatsapp", include_default_mcp_servers=True
        )
        if "brain" not in cli:
            fail(f"{profile}: resolver did not expose Brain to CLI workers")
        if "brain" in telegram or "brain" in whatsapp:
            fail(f"{profile}: resolver exposed Brain outside CLI")
        platform_toolsets = config.get("platform_toolsets") or {}
        if "no_mcp" not in (platform_toolsets.get("telegram") or []):
            fail(f"{profile}: Telegram is missing no_mcp")
        if "no_mcp" not in (platform_toolsets.get("whatsapp") or []):
            fail(f"{profile}: WhatsApp is missing no_mcp")

    if len(set(token_digests.values())) != len(workers):
        fail("Reno and FamaAgent must use distinct Brain credentials")

    for platform in ("cli", "telegram", "whatsapp"):
        if "brain" in _get_platform_tools(
            ceo, platform, include_default_mcp_servers=True
        ):
            fail(f"CEO: resolver exposed Brain on {platform}")

    kanban = ceo.get("kanban") or {}
    if kanban.get("auto_subscribe_on_create") is not True:
        fail("CEO: kanban.auto_subscribe_on_create must be explicitly true")

    smoke_name = "BRAIN_SMOKE_INTERPOLATION_7D3B"
    old_value = os.environ.get(smoke_name)
    try:
        os.environ[smoke_name] = "resolved-smoke-value"
        if _interpolate_env_vars(f"${{{smoke_name}}}") != "resolved-smoke-value":
            fail("Hermes environment interpolation is incompatible")
        del os.environ[smoke_name]
        if _interpolate_env_vars(f"${{{smoke_name}}}") != f"${{{smoke_name}}}":
            fail("Hermes no longer preserves unresolved placeholders")
    finally:
        if old_value is not None:
            os.environ[smoke_name] = old_value
        else:
            os.environ.pop(smoke_name, None)

    kanban_source = (args.hermes_root / "hermes_cli" / "kanban_db.py").read_text(
        encoding="utf-8"
    )
    for assignment in (
        'env["HERMES_KANBAN_TASK"] = task.id',
        'env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)',
    ):
        if assignment not in kanban_source:
            fail(f"Hermes worker capability export changed: {assignment}")

    tool_source = (args.hermes_root / "tools" / "kanban_tools.py").read_text(
        encoding="utf-8"
    )
    required_subscription_evidence = (
        'get_session_env("HERMES_SESSION_PLATFORM"',
        'get_session_env("HERMES_SESSION_CHAT_ID"',
        "_kb.add_notify_sub(",
        "platform=platform, chat_id=chat_id",
    )
    if not all(fragment in tool_source for fragment in required_subscription_evidence):
        fail("Hermes trusted auto-subscription derivation changed")

    state_source = (args.hermes_root / "hermes_state.py").read_text(encoding="utf-8")
    if "_compressed_summary" not in state_source or "compacted" not in state_source:
        fail("Hermes compaction persistence contract changed")

    print(
        "OK: Hermes resolver, headers, worker env and trusted subscription are compatible"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to one safe status
        print(f"FAIL: Hermes integration check failed ({exc})", file=sys.stderr)
        raise SystemExit(1) from None
