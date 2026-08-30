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
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whatsapp_turns (
        wa_turn_id TEXT PRIMARY KEY,
        hermes_session_id TEXT,
        session_key_hmac TEXT,
        contact_key TEXT,
        body_hmac TEXT,
        body_length INTEGER CHECK (body_length IS NULL OR body_length >= 0),
        turn_timestamp REAL,
        correlation_status TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turn_events (
        wa_turn_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (wa_turn_id, event_id),
        UNIQUE (wa_turn_id, ordinal),
        FOREIGN KEY (wa_turn_id) REFERENCES whatsapp_turns(wa_turn_id),
        FOREIGN KEY (event_id) REFERENCES transport_events(event_id)
    )
    """,
    # Identifiers a pending turn is still waiting for. Deliberately has no
    # foreign key to transport_events: the whole purpose is to record a
    # candidate whose event has not been ingested yet. Values are derived
    # HMACs, never a raw observer message id. Rows are deleted once the turn
    # reaches a terminal state.
    """
    CREATE TABLE IF NOT EXISTS turn_candidate_events (
        wa_turn_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        candidate_event_id TEXT NOT NULL,
        PRIMARY KEY (wa_turn_id, ordinal),
        FOREIGN KEY (wa_turn_id) REFERENCES whatsapp_turns(wa_turn_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kanban_bindings (
        task_id TEXT PRIMARY KEY,
        wa_turn_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK (stage IN ('porteiro', 'cadastro', 'reno')),
        created_at REAL NOT NULL,
        FOREIGN KEY (wa_turn_id) REFERENCES whatsapp_turns(wa_turn_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lead_lifecycles (
        lifecycle_id TEXT PRIMARY KEY,
        origin_event_id TEXT NOT NULL UNIQUE,
        wa_turn_id TEXT NOT NULL,
        contact_key TEXT NOT NULL,
        client_id INTEGER NOT NULL UNIQUE CHECK (client_id > 0),
        phase TEXT NOT NULL,
        last_proven_status TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        terminal_at REAL,
        FOREIGN KEY (origin_event_id) REFERENCES transport_events(event_id),
        FOREIGN KEY (wa_turn_id) REFERENCES whatsapp_turns(wa_turn_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lifecycle_facts (
        lifecycle_id TEXT NOT NULL,
        fact_type TEXT NOT NULL,
        evidence_ref TEXT,
        observed_at REAL NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (lifecycle_id, fact_type),
        FOREIGN KEY (lifecycle_id) REFERENCES lead_lifecycles(lifecycle_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lifecycle_effects (
        effect_id TEXT PRIMARY KEY,
        lifecycle_id TEXT NOT NULL,
        expected_status TEXT NOT NULL,
        target_status TEXT NOT NULL,
        cause TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN (
                'pending', 'claimed', 'applied', 'already_applied',
                'superseded', 'conflict', 'retryable', 'permanent_failure'
            )
        ),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        lease_token_hmac TEXT,
        lease_expires_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        FOREIGN KEY (lifecycle_id) REFERENCES lead_lifecycles(lifecycle_id)
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
    CREATE TABLE IF NOT EXISTS reconcile_state (
        name TEXT PRIMARY KEY,
        value TEXT,
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
