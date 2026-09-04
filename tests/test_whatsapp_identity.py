from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.transport_models import RuntimeIds
from brain.whatsapp_identity import (
    PhoneResolution,
    resolve_phone,
    verify_transport_identity,
)


class WhatsAppIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mapping_dir = Path(self.temp_dir.name) / "session"
        self.mapping_dir.mkdir()
        self.transport_ids = RuntimeIds(b"t" * 32)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, name: str, value: object) -> None:
        (self.mapping_dir / name).write_text(json.dumps(value), encoding="utf-8")

    def test_phone_jid_returns_transport_digits(self) -> None:
        result = resolve_phone("5534999772714@s.whatsapp.net", self.mapping_dir)
        self.assertEqual(result, PhoneResolution("ok", "5534999772714", "resolved"))

    def test_forward_mapping_uses_phone_from_filename(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.phone, "5534999772714")

    def test_reverse_mapping_uses_phone_from_content(self) -> None:
        self._write("lid-mapping-123456789012345_reverse.json", "5534999772714")

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.phone, "5534999772714")

    def test_consistent_forward_and_reverse_mappings_are_accepted(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")
        self._write("lid-mapping-123456789012345_reverse.json", "5534999772714")

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result, PhoneResolution("ok", "5534999772714", "resolved"))

    def test_conflicting_mappings_are_unavailable(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")
        self._write("lid-mapping-5534999772715.json", "123456789012345")

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "PHONE_IDENTITY_AMBIGUOUS")

    def test_missing_mapping_is_unavailable(self) -> None:
        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "PHONE_MAPPING_UNAVAILABLE")

    def test_unrelated_invalid_mappings_do_not_poison_requested_resolution(
        self,
    ) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")
        (self.mapping_dir / "lid-mapping-5534999772715.json").write_bytes(b"")
        (self.mapping_dir / "lid-mapping-5534999772716.json").write_text(
            "{not-json", encoding="utf-8"
        )

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result, PhoneResolution("ok", "5534999772714", "resolved"))

    def test_oversized_mapping_is_rejected_before_resolution(self) -> None:
        oversized = b'"123456789"' + (b" " * 4097)
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_bytes(oversized)

        result = resolve_phone("123456789@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "PHONE_MAPPING_INVALID")

    def test_malformed_mapping_is_unavailable(self) -> None:
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_text(
            "{not-json", encoding="utf-8"
        )

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "PHONE_MAPPING_INVALID")

    def test_invalid_mapping_value_is_unavailable(self) -> None:
        self._write("lid-mapping-5534999772714.json", "not-a-lid")

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "PHONE_MAPPING_INVALID")

    def test_groups_broadcast_and_session_keys_do_not_resolve(self) -> None:
        for identifier in (
            "120363000000000000@g.us",
            "status@broadcast",
            "agent:main:whatsapp:dm:5534999772714",
        ):
            with self.subTest(identifier=identifier):
                result = resolve_phone(identifier, self.mapping_dir)
                self.assertEqual(result.status, "unavailable")

    def test_path_traversal_identifier_does_not_read_outside_directory(self) -> None:
        outside = Path(self.temp_dir.name) / "lid-mapping-5534999772714.json"
        outside.write_text('"123456789012345"', encoding="utf-8")

        result = resolve_phone("../123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")

    def test_symlinked_mapping_is_rejected(self) -> None:
        target = self.mapping_dir / "real.json"
        target.write_text('"123456789012345"', encoding="utf-8")
        link = self.mapping_dir / "lid-mapping-5534999772714.json"
        link.symlink_to(target)

        result = resolve_phone("123456789012345@lid", self.mapping_dir)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "PHONE_MAPPING_INVALID")

    def test_transport_identity_proves_direct_pn_evidence(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")
        jid = "5534999772714@s.whatsapp.net"

        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac(jid),
            contact_key=self.transport_ids.contact_key("5534999772714"),
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.phone, "5534999772714")

    def test_transport_identity_proves_lid_mapping(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")
        jid = "123456789012345@lid"

        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac(jid),
            contact_key=None,
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.phone, "5534999772714")

    def test_transport_identity_without_match_is_unavailable(self) -> None:
        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac("5534999772714@s.whatsapp.net"),
            contact_key=None,
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "unavailable")

    def test_transport_identity_conflict_is_unavailable(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")
        self._write("lid-mapping-5534999772715.json", "123456789012345")

        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac("123456789012345@lid"),
            contact_key=None,
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("AMBIGUOUS", result.reason)

    def test_transport_identity_rejects_divergent_contact_key(self) -> None:
        self._write("lid-mapping-5534999772714.json", "123456789012345")

        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac("123456789012345@lid"),
            contact_key=self.transport_ids.contact_key("5534999772715"),
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "unavailable")

    def test_transport_identity_malformed_mapping_is_not_proof(self) -> None:
        (self.mapping_dir / "lid-mapping-5534999772714.json").write_text(
            "{not-json", encoding="utf-8"
        )

        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac("5534999772714@s.whatsapp.net"),
            contact_key=None,
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "unavailable")

    def test_transport_identity_uses_observer_mapping_dir_only(self) -> None:
        other_dir = Path(self.temp_dir.name) / "hermes-session"
        other_dir.mkdir()
        (other_dir / "lid-mapping-5534999772714.json").write_text(
            '"123456789012345"', encoding="utf-8"
        )

        result = verify_transport_identity(
            remote_jid_hmac=self.transport_ids.jid_hmac("5534999772714@s.whatsapp.net"),
            contact_key=None,
            mapping_dir=self.mapping_dir,
            transport_ids=self.transport_ids,
        )

        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()


class PhoneForContactKeyTests(unittest.TestCase):
    PHONE = "5534999772714"
    LID = "123456789012345"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mapping_dir = Path(self.temp_dir.name)
        self.ids = RuntimeIds(b"t" * 32)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_mapping(self, phone: str, lid: str) -> None:
        (self.mapping_dir / f"lid-mapping-{phone}.json").write_text(
            json.dumps(lid), encoding="utf-8"
        )
