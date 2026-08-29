#!/usr/bin/env python3
"""Verify Brain's Hermes-facing contract with the installed Hermes runtime."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

STATE_SCHEMA = {
    "sessions": {"id", "session_key", "source", "chat_id", "chat_type", "started_at"},
    "messages": {
        "id",
        "session_id",
        "role",
        "content",
        "timestamp",
        "active",
        "compacted",
        "display_kind",
        "_compressed_summary",
        "tool_calls",
        "tool_name",
    },
}
KANBAN_SCHEMA = {
    "tasks": {"id", "assignee", "status", "current_run_id", "session_id"},
    "task_runs": {"id", "task_id", "status"},
    "kanban_notify_subs": {
        "task_id",
        "platform",
        "chat_id",
        "chat_type",
        "notifier_profile",
    },
}


def parsed_source(path: Path) -> ast.Module:
    if not path.is_file():
        fail(f"required Hermes source is missing: {path}")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def literal_strings(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def check_sqlite_schema(path: Path, requirements: dict[str, set[str]]) -> None:
    if not path.is_file():
        fail(f"Hermes database is missing: {path.name}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        for table, expected in requirements.items():
            actual = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if not expected.issubset(actual):
                fail(f"Hermes {path.name} schema changed: {table}")
    finally:
        conn.close()


def check_upstream_contracts(hermes_root: Path) -> None:
    from gateway.session_context import _VAR_MAP, get_session_env
    from hermes_cli.plugins import VALID_HOOKS, PluginContext, invoke_hook

    if (
        not callable(invoke_hook)
        or "hook_name" not in inspect.signature(PluginContext.register_hook).parameters
    ):
        fail("Hermes public plugin hook API changed")
    if not {"pre_llm_call", "pre_tool_call"}.issubset(VALID_HOOKS):
        fail("Hermes required hooks are unavailable")

    required_context_fields = {
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_TYPE",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_KEY",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_PROFILE",
    }
    if not callable(get_session_env) or not required_context_fields.issubset(_VAR_MAP):
        fail("Hermes gateway session ContextVar API changed")

    turn_tree = parsed_source(hermes_root / "agent/turn_context.py")
    pre_llm_calls = [
        call
        for call in calls(turn_tree, "_invoke_hook")
        if call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "pre_llm_call"
    ]
    if not pre_llm_calls or "turn_id" not in {
        keyword.arg for keyword in pre_llm_calls[0].keywords
    }:
        fail("Hermes pre_llm_call no longer supplies turn_id")

    plugins_tree = parsed_source(hermes_root / "hermes_cli/plugins.py")
    plugin_literals = literal_strings(plugins_tree)
    if not {"pre_tool_call", "modify", "args"}.issubset(plugin_literals):
        fail("Hermes pre_tool_call modification contract changed")
    if not calls(plugins_tree, "update"):
        fail("Hermes pre_tool_call no longer merges modified arguments")

    adapter_tree = parsed_source(hermes_root / "plugins/platforms/whatsapp/adapter.py")
    adapter_literals = literal_strings(adapter_tree)
    if "\n" not in adapter_literals:
        fail('Hermes WhatsApp batching separator is not exact "\\n"')
    if not calls(adapter_tree, "cancel") or not calls(adapter_tree, "create_task"):
        fail("Hermes WhatsApp debounce timer reset contract changed")
    adapter_classes = {
        node.name for node in ast.walk(adapter_tree) if isinstance(node, ast.ClassDef)
    }
    adapter_methods = {
        node.name
        for node in ast.walk(adapter_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "WhatsAppAdapter" not in adapter_classes or not {
        "_enqueue_text_event",
        "_flush_text_batch",
    }.issubset(adapter_methods):
        fail("Hermes WhatsApp adapter contract changed")

    bridge = hermes_root / "scripts/whatsapp-bridge/bridge.js"
    bridge_source = bridge.read_text(encoding="utf-8")
    identity_source = (hermes_root / "gateway/whatsapp_identity.py").read_text(
        encoding="utf-8"
    )
    if not all(
        fragment in bridge_source
        for fragment in ("SESSION_DIR", "lid-mapping-", "readFileSync", "JSON.parse")
    ) or not all(
        fragment in identity_source for fragment in ("lid-mapping-", "_reverse")
    ):
        fail("Hermes WhatsApp bridge/identity adapter contract changed")

    ledger_path = hermes_root / "gateway/delivery_ledger.py"
    ledger_tree = parsed_source(ledger_path)
    ledger_source = ledger_path.read_text(encoding="utf-8")
    ledger_functions = {
        node.name
        for node in ast.walk(ledger_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required_ledger = {
        "record_obligation",
        "mark_attempting",
        "mark_delivered",
        "mark_failed",
        "sweep_recoverable",
        "ledger_enabled",
    }
    if not required_ledger.issubset(ledger_functions) or not all(
        fragment in ledger_source
        for fragment in (
            "state='pending'",
            "state='attempting'",
            "state='delivered'",
            "state='failed'",
            "delivery_obligations",
        )
    ):
        fail("Hermes delivery ledger semantics changed")


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
        "--porteiro-config",
        type=Path,
        default=Path("/root/.hermes/profiles/porteiro/config.yaml"),
    )
    parser.add_argument(
        "--cadastro-config",
        type=Path,
        default=Path("/root/.hermes/profiles/cadastro/config.yaml"),
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
    hermes_home = Path(os.environ.get("HERMES_HOME", args.ceo_config.parent))
    os.environ.setdefault("HERMES_HOME", str(hermes_home))

    import yaml  # type: ignore[import-not-found]
    from agent.secret_scope import load_env_file
    from hermes_cli.plugin_dev import doctor_plugin
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.tools_config import _get_platform_tools
    from tools.mcp_tool import _interpolate_env_vars

    check_upstream_contracts(args.hermes_root)
    check_sqlite_schema(hermes_home / "state.db", STATE_SCHEMA)
    check_sqlite_schema(hermes_home / "kanban.db", KANBAN_SCHEMA)

    def load(path: Path) -> dict:
        if not path.is_file():
            fail(f"missing config: {path}")
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(parsed, dict):
            fail(f"invalid config mapping: {path}")
        return parsed

    worker_paths = {
        "porteiro": args.porteiro_config,
        "cadastro": args.cadastro_config,
        "reno": args.reno_config,
        "famaagent": args.famaagent_config,
    }
    workers = {profile: load(path) for profile, path in worker_paths.items()}
    ceo = load(args.ceo_config)
    if not args.server_config.is_file():
        fail("Brain server configuration is missing")
    server_config = tomllib.loads(args.server_config.read_text(encoding="utf-8"))
    configured_principals = server_config.get("principals") or {}
    token_digests: dict[str, str] = {}

    expected_headers = {
        "Authorization": "Bearer ${BRAIN_TOKEN}",
        "X-Hermes-Task": "${HERMES_KANBAN_TASK}",
        "X-Hermes-Run": "${HERMES_KANBAN_RUN_ID}",
    }
    expected_tools = {
        "porteiro": {"conversation_phone"},
        "cadastro": {"conversation_phone"},
        "reno": {"conversation_recent", "conversation_search"},
        "famaagent": {"conversation_recent", "conversation_search"},
    }
    expected_modes = {
        "porteiro": "worker",
        "cadastro": "worker",
        "reno": "worker",
        "famaagent": "worker",
        "default": "gateway",
    }
    expected_cli_toolsets = {
        "porteiro": {"brain", "famachat"},
        "cadastro": {"brain", "famachat"},
        "reno": {"brain", "famachat"},
        "famaagent": {"brain"},
    }
    for profile, config in workers.items():
        token = load_env_file(worker_paths[profile].parent / ".env").get("BRAIN_TOKEN")
        principal = configured_principals.get(profile) or {}
        if (
            principal.get("mode") != expected_modes[profile]
            or set(principal.get("tools") or []) != expected_tools[profile]
        ):
            fail(f"{profile}: principal mode or tool ACL changed")
        expected_digest = principal.get("token_sha256")
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
        if (
            set((server.get("tools") or {}).get("include") or [])
            != expected_tools[profile]
        ):
            fail(f"{profile}: Brain tools.include is not the exact V2 allowlist")
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
        if not expected_cli_toolsets[profile].issubset(cli):
            fail(f"{profile}: CLI lost a required MCP toolset")
        if profile == "famaagent" and "famachat" in cli:
            fail("famaagent: FamaChat must not be exposed to this Profile")
        if "brain" in telegram or "brain" in whatsapp:
            fail(f"{profile}: resolver exposed Brain outside CLI")
        platform_toolsets = config.get("platform_toolsets") or {}
        if "no_mcp" not in (platform_toolsets.get("telegram") or []):
            fail(f"{profile}: Telegram is missing no_mcp")
        if "no_mcp" not in (platform_toolsets.get("whatsapp") or []):
            fail(f"{profile}: WhatsApp is missing no_mcp")

    if len(set(token_digests.values())) != len(workers):
        fail("Brain worker principals must use distinct credentials")

    gateway_token = os.environ.get("BRAIN_GATEWAY_TOKEN") or load_env_file(
        hermes_home / ".env"
    ).get("BRAIN_GATEWAY_TOKEN")
    gateway_principal = configured_principals.get("default") or {}
    gateway_tools = set(gateway_principal.get("tools") or [])
    supported_gateway_tools = {
        frozenset({"conversation_phone"}),
        frozenset({"conversation_context", "turn_register"}),
    }
    if (
        gateway_principal.get("mode") != expected_modes["default"]
        or frozenset(gateway_tools) not in supported_gateway_tools
    ):
        fail("default: deployed principal is neither legacy nor Plan 1 compatible")
    gateway_digest = gateway_principal.get("token_sha256")
    if not gateway_token or not isinstance(gateway_digest, str):
        fail("default: Brain gateway credentials are incomplete")
    actual_gateway_digest = hashlib.sha256(gateway_token.encode("utf-8")).hexdigest()
    if actual_gateway_digest != gateway_digest:
        fail("default: gateway token does not match the server digest")
    token_digests["default"] = actual_gateway_digest
    if len(set(token_digests.values())) != len(token_digests):
        fail("Brain worker and gateway principals must use distinct credentials")

    discover_plugins()
    for platform in ("cli", "telegram", "whatsapp"):
        if "brain" in _get_platform_tools(
            ceo, platform, include_default_mcp_servers=True
        ):
            fail(f"CEO: resolver exposed Brain on {platform}")

    enabled_plugins = (ceo.get("plugins") or {}).get("enabled") or []
    if "brain-ceo-bridge" not in enabled_plugins:
        fail("CEO: brain-ceo-bridge is not enabled")
    plugin_path = Path(__file__).parents[1] / "integrations/hermes/brain-ceo-bridge"
    if not plugin_path.is_dir():
        fail(f"CEO: versioned Brain bridge is missing: {plugin_path}")
    report = doctor_plugin(plugin_path)
    if (
        not report.ok
        or set(report.registered_tools) != {"conversation_context"}
        or set(report.registered_hooks) != {"pre_llm_call", "pre_tool_call"}
    ):
        fail("CEO: versioned Brain bridge failed Hermes Plugin Doctor")
    plugin_source = plugin_path / "tools.py"
    if not plugin_source.is_file() or "get_session_env" not in plugin_source.read_text(
        encoding="utf-8"
    ):
        fail("CEO: versioned Brain bridge does not use gateway session context")

    cli = _get_platform_tools(ceo, "cli", include_default_mcp_servers=True)
    telegram = _get_platform_tools(ceo, "telegram", include_default_mcp_servers=True)
    whatsapp = _get_platform_tools(ceo, "whatsapp", include_default_mcp_servers=True)
    if "brain-context" in cli or "brain-context" in telegram:
        fail("CEO: brain-context must be disabled outside WhatsApp")
    if "brain-context" not in whatsapp:
        fail("CEO: brain-context is not enabled for WhatsApp")

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

    print(
        "OK: Hermes plugin hooks, schemas, WhatsApp batching, delivery ledger, "
        "resolver and trusted subscription are compatible"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to one safe status
        print(f"FAIL: Hermes integration check failed ({exc})", file=sys.stderr)
        raise SystemExit(1) from None
