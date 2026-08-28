"""Short-lived, strictly read-only SQLite access to Hermes databases."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .errors import DatabaseUnavailable

T = TypeVar("T")

SCHEMA_REQUIREMENTS: dict[str, dict[str, set[str]]] = {
    "kanban": {
        "tasks": {"id", "assignee", "status", "current_run_id", "session_id"},
        "task_runs": {"id", "task_id", "status"},
        "kanban_notify_subs": {
            "task_id",
            "platform",
            "chat_id",
            "chat_type",
            "notifier_profile",
        },
    },
    "state": {
        "sessions": {
            "id",
            "session_key",
            "source",
            "chat_id",
            "chat_type",
            "started_at",
        },
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
    },
}


def _is_busy(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class ReadOnlyDatabase:
    def __init__(
        self,
        path: Path,
        *,
        retries: int = 2,
        timeout_seconds: float = 1.0,
    ) -> None:
        self.path = path
        self.retries = retries
        self.timeout_seconds = timeout_seconds

    def connect(self) -> sqlite3.Connection:
        resolved = self.path.expanduser().resolve()
        if not resolved.is_file():
            raise DatabaseUnavailable()
        uri = resolved.as_uri() + "?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
            # This is a defense-in-depth application guard. The URI mode=ro is
            # the actual filesystem/database protection; this callback also
            # prevents accidental mutations if a future query is added.
            write_actions = {
                getattr(sqlite3, name)
                for name in (
                    "SQLITE_INSERT",
                    "SQLITE_UPDATE",
                    "SQLITE_DELETE",
                    "SQLITE_CREATE_INDEX",
                    "SQLITE_CREATE_TABLE",
                    "SQLITE_CREATE_TEMP_INDEX",
                    "SQLITE_CREATE_TEMP_TABLE",
                    "SQLITE_CREATE_TEMP_TRIGGER",
                    "SQLITE_CREATE_TEMP_VIEW",
                    "SQLITE_CREATE_TRIGGER",
                    "SQLITE_CREATE_VIEW",
                    "SQLITE_DROP_INDEX",
                    "SQLITE_DROP_TABLE",
                    "SQLITE_DROP_TEMP_INDEX",
                    "SQLITE_DROP_TEMP_TABLE",
                    "SQLITE_DROP_TEMP_TRIGGER",
                    "SQLITE_DROP_TEMP_VIEW",
                    "SQLITE_DROP_TRIGGER",
                    "SQLITE_DROP_VIEW",
                    "SQLITE_ALTER_TABLE",
                    "SQLITE_ATTACH",
                    "SQLITE_DETACH",
                )
                if hasattr(sqlite3, name)
            }

            def deny_writes(
                action: int,
                _arg1: str | None,
                _arg2: str | None,
                _db: str | None,
                _source: str | None,
            ) -> int:
                return (
                    sqlite3.SQLITE_DENY
                    if action in write_actions
                    else sqlite3.SQLITE_OK
                )

            conn.set_authorizer(deny_writes)
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            # Preserve SQLITE_BUSY/LOCKED so read() can apply its bounded
            # retry policy to failures that happen while opening/configuring
            # the connection, not only while executing the callback.
            if _is_busy(exc):
                raise
            raise DatabaseUnavailable() from exc
        except OSError as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise DatabaseUnavailable() from exc

    def read(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        for attempt in range(self.retries + 1):
            conn: sqlite3.Connection | None = None
            try:
                conn = self.connect()
                return callback(conn)
            except DatabaseUnavailable:
                raise
            except sqlite3.Error as exc:
                if _is_busy(exc) and attempt < self.retries:
                    time.sleep(0.03 * (attempt + 1))
                    continue
                raise DatabaseUnavailable() from exc
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
        raise DatabaseUnavailable()  # pragma: no cover


class SchemaGuard:
    def __init__(self, state: ReadOnlyDatabase, kanban: ReadOnlyDatabase) -> None:
        self.state = state
        self.kanban = kanban

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _check_db(
        self, db: ReadOnlyDatabase, requirements: dict[str, set[str]]
    ) -> bool:
        def check(conn: sqlite3.Connection) -> bool:
            return all(
                expected.issubset(self._table_columns(conn, table))
                for table, expected in requirements.items()
            )

        return db.read(check)

    def check(self) -> bool:
        try:
            return self._check_db(
                self.state, SCHEMA_REQUIREMENTS["state"]
            ) and self._check_db(self.kanban, SCHEMA_REQUIREMENTS["kanban"])
        except DatabaseUnavailable:
            return False
