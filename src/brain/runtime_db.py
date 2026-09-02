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
)


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
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

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
