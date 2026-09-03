"""Writable SQLite persistence owned exclusively by Brain."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS transport_events (
        event_id TEXT PRIMARY KEY,
        observer_device_id TEXT NOT NULL,
        contact_key TEXT,
        direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
        received_at REAL NOT NULL,
        message_timestamp REAL,
        body_hmac TEXT,
        body_length INTEGER CHECK (body_length IS NULL OR body_length >= 0),
        native_type TEXT,
        transport_kind TEXT NOT NULL,
        source_type TEXT,
        source_app TEXT,
        source_id_present INTEGER
            CHECK (source_id_present IS NULL OR source_id_present IN (0, 1)),
        source_id_length INTEGER
            CHECK (source_id_length IS NULL OR source_id_length >= 0),
        source_id_hmac TEXT,
        source_url_hostname TEXT,
        source_url_length INTEGER
            CHECK (source_url_length IS NULL OR source_url_length >= 0),
        source_url_hmac TEXT,
        ctwa_clid_present INTEGER
            CHECK (ctwa_clid_present IS NULL OR ctwa_clid_present IN (0, 1)),
        ctwa_clid_length INTEGER
            CHECK (ctwa_clid_length IS NULL OR ctwa_clid_length >= 0),
        ctwa_clid_hmac TEXT,
        show_ad_attribution INTEGER
            CHECK (show_ad_attribution IS NULL OR show_ad_attribution IN (0, 1)),
        click_to_whatsapp_call INTEGER
            CHECK (click_to_whatsapp_call IS NULL OR click_to_whatsapp_call IN (0, 1)),
        contains_auto_reply INTEGER
            CHECK (contains_auto_reply IS NULL OR contains_auto_reply IN (0, 1)),
        external_ad_reply_raw_json TEXT,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contact_ephemera (
        contact_key TEXT PRIMARY KEY,
        display_name TEXT,
        display_name_hmac TEXT,
        expires_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ctwa_meta_attributions (
        event_id TEXT PRIMARY KEY REFERENCES transport_events(event_id) ON DELETE CASCADE,
        account_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        ctwa_clid TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending','confirmed','unavailable')),
        ad_id TEXT, ad_name TEXT, campaign_id TEXT, campaign_name TEXT,
        ad_status TEXT, ad_effective_status TEXT,
        campaign_status TEXT, campaign_effective_status TEXT,
        match_method TEXT CHECK(match_method IS NULL OR match_method='source_id_exact'),
        reason_code TEXT, confirmed_at REAL, last_attempt_at REAL,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        next_attempt_at REAL, lease_until REAL, lease_token TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_attribution_jobs (
        account_id TEXT NOT NULL, source_id TEXT NOT NULL,
        next_attempt_at REAL NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error_code TEXT, lease_until REAL, lease_token TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(account_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_attribution_state (
        account_id TEXT PRIMARY KEY, auth_circuit_until REAL NOT NULL DEFAULT 0,
        last_probe_at REAL, last_success_at REAL, updated_at REAL NOT NULL
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_ctwa_meta_attributions_status_due "
        "ON ctwa_meta_attributions(status, next_attempt_at)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_ctwa_meta_attributions_source_status "
        "ON ctwa_meta_attributions(source_id, status)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_transport_events_contact_received "
        "ON transport_events(contact_key, received_at)"
    ),
)

_ATTRIBUTION_COLUMN_DEFINITIONS = {
    "ctwa_meta_attributions": (
        ("event_id", "TEXT", 0, None, 1),
        ("account_id", "TEXT", 1, None, 0),
        ("source_id", "TEXT", 1, None, 0),
        ("ctwa_clid", "TEXT", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("ad_id", "TEXT", 0, None, 0),
        ("ad_name", "TEXT", 0, None, 0),
        ("campaign_id", "TEXT", 0, None, 0),
        ("campaign_name", "TEXT", 0, None, 0),
        ("ad_status", "TEXT", 0, None, 0),
        ("ad_effective_status", "TEXT", 0, None, 0),
        ("campaign_status", "TEXT", 0, None, 0),
        ("campaign_effective_status", "TEXT", 0, None, 0),
        ("match_method", "TEXT", 0, None, 0),
        ("reason_code", "TEXT", 0, None, 0),
        ("confirmed_at", "REAL", 0, None, 0),
        ("last_attempt_at", "REAL", 0, None, 0),
        ("attempt_count", "INTEGER", 1, "0", 0),
        ("next_attempt_at", "REAL", 0, None, 0),
        ("lease_until", "REAL", 0, None, 0),
        ("lease_token", "TEXT", 0, None, 0),
        ("created_at", "REAL", 1, None, 0),
        ("updated_at", "REAL", 1, None, 0),
    ),
    "meta_attribution_jobs": (
        ("account_id", "TEXT", 1, None, 1),
        ("source_id", "TEXT", 1, None, 2),
        ("next_attempt_at", "REAL", 1, None, 0),
        ("attempt_count", "INTEGER", 1, "0", 0),
        ("last_error_code", "TEXT", 0, None, 0),
        ("lease_until", "REAL", 0, None, 0),
        ("lease_token", "TEXT", 0, None, 0),
        ("created_at", "REAL", 1, None, 0),
        ("updated_at", "REAL", 1, None, 0),
    ),
    "meta_attribution_state": (
        ("account_id", "TEXT", 0, None, 1),
        ("auth_circuit_until", "REAL", 1, "0", 0),
        ("last_probe_at", "REAL", 0, None, 0),
        ("last_success_at", "REAL", 0, None, 0),
        ("updated_at", "REAL", 1, None, 0),
    ),
}

_ATTRIBUTION_CONSTRAINTS = {
    "ctwa_meta_attributions": (
        "check(statusin('pending','confirmed','unavailable'))",
        "check(match_methodisnullormatch_method='source_id_exact')",
        "check(attempt_count>=0)",
    ),
    "meta_attribution_jobs": (),
    "meta_attribution_state": (),
}

_ATTRIBUTION_PRIMARY_KEYS = {
    "ctwa_meta_attributions": ("event_id",),
    "meta_attribution_jobs": ("account_id", "source_id"),
    "meta_attribution_state": ("account_id",),
}

_ATTRIBUTION_INDEXES = {
    "idx_ctwa_meta_attributions_status_due": (
        "ctwa_meta_attributions",
        ("status", "next_attempt_at"),
    ),
    "idx_ctwa_meta_attributions_source_status": (
        "ctwa_meta_attributions",
        ("source_id", "status"),
    ),
    "idx_transport_events_contact_received": (
        "transport_events",
        ("contact_key", "received_at"),
    ),
}


class RuntimeDatabase:
    """Short-lived transactional access to Brain's own writable database."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise sqlite3.OperationalError("runtime database did not enable WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    conn.execute(statement)
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(transport_events)")
                }
                if "external_ad_reply_raw_json" not in columns:
                    conn.execute(
                        "ALTER TABLE transport_events "
                        "ADD COLUMN external_ad_reply_raw_json TEXT"
                    )
                self._validate_attribution_schema(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

    @staticmethod
    def _validate_attribution_schema(conn: sqlite3.Connection) -> None:
        """Reject legacy lookalikes that would silently drop durable invariants."""
        for table, expected_columns in _ATTRIBUTION_COLUMN_DEFINITIONS.items():
            column_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            actual_columns = tuple(
                (
                    str(row[1]),
                    str(row[2]).upper(),
                    int(row[3]),
                    None if row[4] is None else str(row[4]),
                    int(row[5]),
                )
                for row in column_info
            )
            if actual_columns != expected_columns:
                raise sqlite3.OperationalError(
                    f"incompatible runtime attribution column declaration for {table}"
                )
            primary_key = tuple(
                str(row[1])
                for row in sorted(column_info, key=lambda row: int(row[5]))
                if int(row[5])
            )
            if primary_key != _ATTRIBUTION_PRIMARY_KEYS[table]:
                raise sqlite3.OperationalError(
                    f"incompatible runtime attribution primary key for {table}"
                )
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            sql = "" if sql_row is None or sql_row[0] is None else str(sql_row[0])
            normalized = "".join(sql.lower().split())
            if any(
                constraint not in normalized
                for constraint in _ATTRIBUTION_CONSTRAINTS[table]
            ):
                raise sqlite3.OperationalError(
                    f"incompatible runtime attribution constraints for {table}"
                )
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(ctwa_meta_attributions)"
        ).fetchall()
        if not any(
            row[2] == "transport_events"
            and row[3] == "event_id"
            and row[4] == "event_id"
            and str(row[5]).upper() == "NO ACTION"
            and str(row[6]).upper() == "CASCADE"
            and str(row[7]).upper() == "NONE"
            for row in foreign_keys
        ):
            raise sqlite3.OperationalError(
                "ctwa attribution parent cascade is incompatible"
            )
        for index_name, (table, expected_columns) in _ATTRIBUTION_INDEXES.items():
            index_rows = [
                row
                for row in conn.execute(f"PRAGMA index_list({table})")
                if str(row[1]) == index_name
            ]
            if len(index_rows) != 1:
                raise sqlite3.OperationalError(
                    f"missing required runtime attribution index {index_name}"
                )
            index = index_rows[0]
            if int(index[2]) or str(index[3]).lower() != "c" or int(index[4]):
                raise sqlite3.OperationalError(
                    f"incompatible runtime attribution index {index_name}"
                )
            actual_columns = tuple(
                str(row[2]) for row in conn.execute(f"PRAGMA index_info({index_name})")
            )
            if actual_columns != expected_columns:
                raise sqlite3.OperationalError(
                    f"incompatible runtime attribution index {index_name}"
                )

    def read(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            return callback(conn)
        finally:
            conn.close()

    def write(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = callback(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
