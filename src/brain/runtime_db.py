"""Writable SQLite persistence owned exclusively by Brain."""

from __future__ import annotations

import math
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
    CREATE TABLE IF NOT EXISTS meta_ads_catalog (
        account_id TEXT NOT NULL,
        ad_id TEXT NOT NULL,
        ad_name TEXT NOT NULL,
        ad_status TEXT,
        ad_effective_status TEXT,
        adset_id TEXT,
        adset_name TEXT,
        adset_status TEXT,
        campaign_id TEXT NOT NULL,
        campaign_name TEXT NOT NULL,
        campaign_status TEXT,
        creative_id TEXT,
        creative_name TEXT,
        metadata_complete INTEGER NOT NULL
            CHECK (metadata_complete IN (0, 1)),
        fetched_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        PRIMARY KEY (account_id, ad_id),
        CHECK ((adset_id IS NULL) = (adset_name IS NULL)),
        CHECK ((creative_id IS NULL) = (creative_name IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ctwa_meta_attributions (
        event_id TEXT PRIMARY KEY
            REFERENCES transport_events(event_id) ON DELETE CASCADE,
        account_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        ctwa_clid TEXT,
        status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')),
        matched_ad_id TEXT,
        match_method TEXT,
        metadata_complete INTEGER NOT NULL CHECK (metadata_complete IN (0, 1)),
        confirmed_at REAL,
        last_attempt_at REAL,
        last_error_code TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (
            last_error_code IS NULL
            OR last_error_code IN (
                'meta_timeout', 'meta_rate_limited', 'meta_server_unavailable',
                'meta_auth_unavailable', 'meta_required_tool_unavailable',
                'meta_not_found', 'meta_incomplete_result', 'meta_account_mismatch',
                'meta_invalid_response'
            )
        ),
        CHECK (
            status = 'pending'
            OR (
                matched_ad_id = source_id
                AND match_method = 'source_id_exact'
                AND confirmed_at IS NOT NULL
            )
        ),
        CHECK (
            status = 'pending'
            OR matched_ad_id IS NOT NULL
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_attribution_jobs (
        account_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at REAL NOT NULL,
        lease_until REAL,
        lease_token TEXT,
        last_error_code TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (account_id, source_id),
        CHECK (
            last_error_code IS NULL
            OR last_error_code IN (
                'meta_timeout', 'meta_rate_limited', 'meta_server_unavailable',
                'meta_auth_unavailable', 'meta_required_tool_unavailable',
                'meta_not_found', 'meta_incomplete_result', 'meta_account_mismatch',
                'meta_invalid_response'
            )
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_attribution_state (
        account_id TEXT PRIMARY KEY,
        auth_circuit_until REAL NOT NULL CHECK (auth_circuit_until >= 0),
        auth_credential_fingerprint TEXT,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meta_attribution_jobs_due
    ON meta_attribution_jobs(next_attempt_at, lease_until)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ctwa_meta_attributions_lookup
    ON ctwa_meta_attributions(account_id, source_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_meta_ads_catalog_gc
    ON meta_ads_catalog(last_seen_at)
    """,
)


class RuntimeDatabase:
    """Short-lived transactional access to Brain's own writable database."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _validated_timeout(timeout_seconds: float) -> float:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        return float(timeout_seconds)

    def _connect(self, *, timeout_seconds: float | None = None) -> sqlite3.Connection:
        timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else self._validated_timeout(timeout_seconds)
        )
        conn = sqlite3.connect(str(self.path), timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
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
                job_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(meta_attribution_jobs)")
                }
                if "lease_token" not in job_columns:
                    conn.execute(
                        "ALTER TABLE meta_attribution_jobs ADD COLUMN lease_token TEXT"
                    )
                state_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(meta_attribution_state)")
                }
                if "auth_credential_fingerprint" not in state_columns:
                    conn.execute(
                        "ALTER TABLE meta_attribution_state "
                        "ADD COLUMN auth_credential_fingerprint TEXT"
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

    def read(
        self,
        callback: Callable[[sqlite3.Connection], T],
        *,
        timeout_seconds: float | None = None,
    ) -> T:
        conn = self._connect(timeout_seconds=timeout_seconds)
        try:
            return callback(conn)
        finally:
            conn.close()

    def write(
        self,
        callback: Callable[[sqlite3.Connection], T],
        *,
        timeout_seconds: float | None = None,
    ) -> T:
        conn = self._connect(timeout_seconds=timeout_seconds)
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
