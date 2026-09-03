from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.db import ReadOnlyDatabase
from brain.runtime_db import RuntimeDatabase
from brain.service import BrainService
from brain.transport_models import RuntimeIds

BUSINESS_TABLES = {
    "transport_events",
    "contact_ephemera",
    "ctwa_meta_attributions",
    "meta_attribution_jobs",
    "meta_attribution_state",
}


class RuntimeDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "nested" / "brain-runtime.db"
        self.runtime = RuntimeDatabase(self.path, timeout_seconds=0.25)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def initialize(self) -> None:
        self.runtime.initialize()

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "INSERT INTO transport_events "
            "(event_id, observer_device_id, direction, received_at, "
            "transport_kind, created_at) VALUES (?, 'observer-a', 'inbound', "
            "1.0, 'ordinary', 1.0)",
            (event_id,),
        )

    def test_initialize_creates_exact_business_tables_and_parent(self) -> None:
        self.assertFalse(self.path.parent.exists())

        self.initialize()

        tables = self.runtime.read(
            lambda conn: {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        )
        self.assertEqual(tables, BUSINESS_TABLES)
        self.assertTrue(self.path.is_file())

    def test_initialize_is_idempotent_and_preserves_data(self) -> None:
        self.initialize()
        self.runtime.write(lambda conn: self._insert_event(conn, "event-kept"))

        self.initialize()

        kept = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT event_id FROM transport_events"
            ).fetchone()[0]
        )
        self.assertEqual(kept, "event-kept")

    def test_initialize_adds_raw_json_to_a_legacy_transport_table(self) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE transport_events ("
                "event_id TEXT PRIMARY KEY, observer_device_id TEXT NOT NULL, "
                "contact_key TEXT, direction TEXT NOT NULL, received_at REAL NOT NULL, "
                "message_timestamp REAL, body_hmac TEXT, body_length INTEGER, "
                "native_type TEXT, transport_kind TEXT NOT NULL, source_type TEXT, "
                "source_app TEXT, source_id_present INTEGER, source_id_length INTEGER, "
                "source_id_hmac TEXT, source_url_hostname TEXT, source_url_length INTEGER, "
                "source_url_hmac TEXT, ctwa_clid_present INTEGER, ctwa_clid_length INTEGER, "
                "ctwa_clid_hmac TEXT, show_ad_attribution INTEGER, "
                "click_to_whatsapp_call INTEGER, contains_auto_reply INTEGER, "
                "created_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO transport_events "
                "(event_id, observer_device_id, direction, received_at, transport_kind, "
                "created_at) VALUES ('legacy-event', 'observer-a', 'inbound', 1.0, "
                "'ordinary_inbound', 1.0)"
            )
            conn.commit()
        finally:
            conn.close()

        self.initialize()

        columns = self.runtime.read(
            lambda conn: {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(transport_events)")
            }
        )
        self.assertIn("external_ad_reply_raw_json", columns)
        row = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT event_id, external_ad_reply_raw_json FROM transport_events"
            ).fetchone()
        )
        self.assertEqual(tuple(row), ("legacy-event", None))

    def test_wal_foreign_keys_and_writable_runtime_connection(self) -> None:
        self.initialize()

        pragmas = self.runtime.read(
            lambda conn: (
                conn.execute("PRAGMA journal_mode").fetchone()[0],
                conn.execute("PRAGMA foreign_keys").fetchone()[0],
                conn.execute("PRAGMA query_only").fetchone()[0],
            )
        )

        self.assertEqual(pragmas, ("wal", 1, 0))

    def test_attribution_schema_has_required_indexes_and_foreign_key_cascade(
        self,
    ) -> None:
        self.initialize()
        self.runtime.write(lambda conn: self._insert_event(conn, "event-cascade"))
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO ctwa_meta_attributions "
                "(event_id, account_id, source_id, status, created_at, updated_at) "
                "VALUES ('event-cascade', 'act_1598606388477916', '101', 'pending', 1.0, 1.0)"
            )
        )
        index_names = self.runtime.read(
            lambda conn: {
                str(row[1])
                for row in conn.execute("PRAGMA index_list(ctwa_meta_attributions)")
            }
        )
        self.assertTrue(
            {
                "idx_ctwa_meta_attributions_status_due",
                "idx_ctwa_meta_attributions_source_status",
            }.issubset(index_names)
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "DELETE FROM transport_events WHERE event_id = 'event-cascade'"
            )
        )
        self.assertEqual(
            self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM ctwa_meta_attributions"
                ).fetchone()[0]
            ),
            0,
        )

    def test_initialize_rejects_incompatible_existing_attribution_schema(self) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE ctwa_meta_attributions (event_id TEXT PRIMARY KEY)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_initialize_rejects_attribution_job_table_without_its_composite_key(
        self,
    ) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE meta_attribution_jobs ("
                "account_id TEXT NOT NULL, source_id TEXT NOT NULL, "
                "next_attempt_at REAL NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, "
                "last_error_code TEXT, lease_until REAL, lease_token TEXT, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_initialize_rejects_nullable_or_mistyped_job_columns(self) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE meta_attribution_jobs ("
                "account_id TEXT, source_id BLOB NOT NULL, "
                "next_attempt_at TEXT NOT NULL, attempt_count TEXT NOT NULL DEFAULT '0', "
                "last_error_code TEXT, lease_until REAL, lease_token TEXT, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "PRIMARY KEY(account_id, source_id))"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_initialize_rejects_changed_attribution_default(self) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE ctwa_meta_attributions ("
                "event_id TEXT PRIMARY KEY REFERENCES transport_events(event_id) ON DELETE CASCADE, "
                "account_id TEXT NOT NULL, source_id TEXT NOT NULL, ctwa_clid TEXT, "
                "status TEXT NOT NULL CHECK(status IN ('pending','confirmed','unavailable')), "
                "ad_id TEXT, ad_name TEXT, campaign_id TEXT, campaign_name TEXT, "
                "ad_status TEXT, ad_effective_status TEXT, campaign_status TEXT, "
                "campaign_effective_status TEXT, "
                "match_method TEXT CHECK(match_method IS NULL OR match_method='source_id_exact'), "
                "reason_code TEXT, confirmed_at REAL, last_attempt_at REAL, "
                "attempt_count INTEGER NOT NULL DEFAULT 1 CHECK(attempt_count >= 0), "
                "next_attempt_at REAL, lease_until REAL, lease_token TEXT, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_initialize_rejects_weakened_attribution_check(self) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE ctwa_meta_attributions ("
                "event_id TEXT PRIMARY KEY REFERENCES transport_events(event_id) ON DELETE CASCADE, "
                "account_id TEXT NOT NULL, source_id TEXT NOT NULL, ctwa_clid TEXT, "
                "status TEXT NOT NULL CHECK(status IN ('pending','confirmed','unavailable')), "
                "ad_id TEXT, ad_name TEXT, campaign_id TEXT, campaign_name TEXT, "
                "ad_status TEXT, ad_effective_status TEXT, campaign_status TEXT, "
                "campaign_effective_status TEXT, "
                "match_method TEXT CHECK(match_method IS NULL OR match_method='source_id_exact'), "
                "reason_code TEXT, confirmed_at REAL, last_attempt_at REAL, "
                "attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= -1), "
                "next_attempt_at REAL, lease_until REAL, lease_token TEXT, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_initialize_rejects_weakened_check_hidden_by_a_comment(self) -> None:
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE ctwa_meta_attributions ("
                "event_id TEXT PRIMARY KEY REFERENCES transport_events(event_id) ON DELETE CASCADE, "
                "account_id TEXT NOT NULL, source_id TEXT NOT NULL, ctwa_clid TEXT, "
                "status TEXT NOT NULL CHECK(status IN ('pending','confirmed','unavailable')), "
                "ad_id TEXT, ad_name TEXT, campaign_id TEXT, campaign_name TEXT, "
                "ad_status TEXT, ad_effective_status TEXT, campaign_status TEXT, "
                "campaign_effective_status TEXT, "
                "match_method TEXT CHECK(match_method IS NULL OR match_method='source_id_exact'), "
                "reason_code TEXT, confirmed_at REAL, last_attempt_at REAL, "
                "attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= -1) "
                "/* CHECK(attempt_count >= 0) */, next_attempt_at REAL, "
                "lease_until REAL, lease_token TEXT, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_initialize_rejects_conflicting_named_attribution_index(self) -> None:
        self.initialize()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP INDEX idx_ctwa_meta_attributions_status_due")
            conn.execute(
                "CREATE INDEX idx_ctwa_meta_attributions_status_due "
                "ON ctwa_meta_attributions(source_id, status)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()

    def test_write_commits_on_success(self) -> None:
        self.initialize()

        result = self.runtime.write(
            lambda conn: (
                self._insert_event(conn, "event-committed"),
                "committed",
            )[1]
        )

        self.assertEqual(result, "committed")
        self.assertEqual(
            self.runtime.read(
                lambda conn: conn.execute(
                    "SELECT event_id FROM transport_events"
                ).fetchone()[0]
            ),
            "event-committed",
        )

    def test_write_rolls_back_and_propagates_exception(self) -> None:
        self.initialize()

        def fail_after_insert(conn: sqlite3.Connection) -> None:
            self._insert_event(conn, "event-rolled-back")
            raise RuntimeError("expected test failure")

        with self.assertRaisesRegex(RuntimeError, "expected test failure"):
            self.runtime.write(fail_after_insert)

        count = self.runtime.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM transport_events"
            ).fetchone()[0]
        )
        self.assertEqual(count, 0)


class RuntimeIdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport_secret = b"t" * 32
        self.ids = RuntimeIds(self.transport_secret)

    @staticmethod
    def values(ids: RuntimeIds) -> dict[str, str]:
        return {
            "contact": ids.contact_key("5534999999999"),
            "event": ids.event_id("observer-a", "message-a"),
            "body": ids.body_hmac("texto exato"),
            "jid": ids.jid_hmac("opaque-jid"),
            "opaque": ids.opaque_hmac("opaque-value"),
        }

    def test_ids_are_deterministic_and_have_stable_prefixes(self) -> None:
        first = self.values(self.ids)
        second = self.values(RuntimeIds(self.transport_secret))

        self.assertEqual(first, second)
        self.assertTrue(first["event"].startswith("waevt_"))
        self.assertEqual(len(first["body"]), 64)

    def test_every_id_lives_in_the_transport_domain(self) -> None:
        """Amendment 2 left one secret; changing it must move every ID."""
        baseline = self.values(self.ids)
        changed = self.values(RuntimeIds(b"T" * 32))

        for name in baseline:
            with self.subTest(name):
                self.assertNotEqual(baseline[name], changed[name])

    def test_method_domains_are_distinct(self) -> None:
        self.assertNotEqual(self.ids.body_hmac("value"), self.ids.opaque_hmac("value"))
        self.assertNotEqual(self.ids.jid_hmac("value"), self.ids.opaque_hmac("value"))

    def test_event_id_uses_unambiguous_framing_and_device_identity(self) -> None:
        self.assertNotEqual(self.ids.event_id("ab", "c"), self.ids.event_id("a", "bc"))
        self.assertNotEqual(
            self.ids.event_id("observer-a", "message-a"),
            self.ids.event_id("observer-b", "message-a"),
        )

    def test_body_hmac_preserves_exact_unicode_text(self) -> None:
        self.assertNotEqual(self.ids.body_hmac("texto"), self.ids.body_hmac("texto "))
        self.assertNotEqual(self.ids.body_hmac("ação"), self.ids.body_hmac("acão"))

    def test_short_secrets_are_rejected(self) -> None:
        for invalid in (b"short", b"", b"x" * 31):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                RuntimeIds(invalid)

    def test_contact_key_requires_canonical_phone(self) -> None:
        for invalid in ("", "+5534999999999", "05534999999999", "not-a-phone"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.ids.contact_key(invalid)


class BrainServiceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.principals = {
            "default": PrincipalConfig(
                "default",
                "gateway",
                token_digest("gateway"),
                frozenset({"conversation_phone"}),
            )
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_service_initializes_runtime_when_stable_secrets_exist(self) -> None:
        settings = BrainSettings(
            state_db=self.root / "state.db",
            kanban_db=self.root / "kanban.db",
            runtime_db=self.root / "runtime" / "brain-runtime.db",
            principals=self.principals,
            cursor_secret=b"c" * 32,
            transport_hmac_secret=b"t" * 32,
        )

        service = BrainService(settings)

        self.assertIsInstance(service.state, ReadOnlyDatabase)
        self.assertIsInstance(service.kanban, ReadOnlyDatabase)
        self.assertIsInstance(service.runtime, RuntimeDatabase)
        self.assertIsInstance(service.runtime_ids, RuntimeIds)
        self.assertTrue(settings.runtime_db.is_file())

    def test_direct_test_settings_without_secrets_do_not_write_runtime(self) -> None:
        runtime_path = self.root / "runtime" / "brain-runtime.db"
        settings = BrainSettings(
            state_db=self.root / "state.db",
            kanban_db=self.root / "kanban.db",
            runtime_db=runtime_path,
            principals=self.principals,
            cursor_secret=b"c" * 32,
        )

        service = BrainService(settings)

        self.assertIsInstance(service.runtime, RuntimeDatabase)
        self.assertIsNone(service.runtime_ids)
        self.assertFalse(runtime_path.exists())


if __name__ == "__main__":
    unittest.main()
