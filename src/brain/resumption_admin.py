"""Local operator interface. Dry-run by default; never register as an agent tool.

OS administrator access is the trust boundary, not an environment flag or an
ID in a prompt. Worker detection is defense-in-depth, not a root sandbox.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path

from .config import BrainSettings
from .errors import BrainError
from .resumption import ResumptionRegistry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "status", "revoke"])
    parser.add_argument("task_id")
    parser.add_argument("--parent")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--authorization-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--attest-authenticated-control",
        action="store_true",
        help="Local operator attests the previously authenticated Telegram authorization; not a new commercial authorization.",
    )
    args = parser.parse_args(argv)
    try:
        if (args.apply or args.action == "revoke") and (
            os.geteuid() != 0
            or os.environ.get("HERMES_KANBAN_TASK")
            or os.environ.get("HERMES_KANBAN_RUN_ID")
            or not args.apply
            or not args.attest_authenticated_control
        ):
            raise BrainError("AUTH_LOCAL_ADMIN_REQUIRED")
        if args.action == "inspect" and not args.parent:
            raise BrainError("AUTH_RESUMPTION_PARENT_REQUIRED")
        if args.apply and args.action == "status":
            raise BrainError("AUTH_RESUMPTION_DENIED")
        settings = BrainSettings.from_env(args.config)
        registry = ResumptionRegistry(settings)
        result: dict[str, object]
        if args.action == "status":
            result = registry.status(args.task_id)
        elif args.action == "revoke":
            registry.revoke(args.task_id)
            result = registry.status(args.task_id)
        elif args.apply:
            result = registry.issue(args.task_id, args.parent, args.authorization_id)
            # Exact readback, no destination or session identifiers in stdout.
            result["grant"] = registry.status(args.task_id)
        else:
            registry.inspect(args.task_id, args.parent)
            result = {
                "status": "eligible_metadata_only",
                "task_id": args.task_id,
                "parent_id": args.parent,
                "grant_created": False,
                "live_repair_verified": False,
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BrainError, sqlite3.Error, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, BrainError) else "RESUMPTION_UNAVAILABLE"
        print(json.dumps({"status": "denied", "code": code}))
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
