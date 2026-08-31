from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.lifecycle_api import LifecycleClaimService
from brain.lifecycle_models import EM_ATENDIMENTO, NAO_RESPONDEU, SEM_ATENDIMENTO
from brain.runtime_db import RuntimeDatabase
from brain.transport_models import RuntimeIds

ORIGIN_TURN = "waturn_" + "a" * 64
PHONE = "5534999772714"


class ClaimServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RuntimeDatabase(
            Path(self.temp_dir.name) / "runtime.db", timeout_seconds=1.0
        )
        self.runtime.initialize()
        self.ids = RuntimeIds(b"r" * 32, b"t" * 32)
        self.contact_key = self.ids.contact_key(PHONE)
        self.resolvable = True
        self.now = 5000.0
        self.service = self.build()
        self.seed()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def build(self, **kwargs) -> LifecycleClaimService:
        options = {
            "resolve_phone": lambda key: PHONE if self.resolvable else None,
            "lease_seconds": 120.0,
            "clock": lambda: self.now,
        }
        options.update(kwargs)
        return LifecycleClaimService(self.runtime, self.ids, **options)

    def seed(
        self,
        *,
        expected: str = SEM_ATENDIMENTO,
        target: str = NAO_RESPONDEU,
        phase: str = "active",
    ) -> None:
        origin = self.ids.event_id("observer-a", "3EB0CTWA")
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO transport_events (event_id, observer_device_id, "
                "contact_key, direction, received_at, message_timestamp, body_hmac, "
                "body_length, native_type, transport_kind, created_at) "
                "VALUES (?, 'observer-a', ?, 'inbound', 1000.1, 1000.0, 'h', 2, "
                "'extendedTextMessage', 'ctwa_candidate', 1000.0)",
                (origin, self.contact_key),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO whatsapp_turns (wa_turn_id, hermes_session_id, "
                "session_key_hmac, contact_key, body_hmac, body_length, "
                "turn_timestamp, correlation_status, created_at) "
                "VALUES (?, 'g-1', 'k', ?, 'y', 2, 1000.0, 'correlated', 1000.0)",
                (ORIGIN_TURN, self.contact_key),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO lead_lifecycles (lifecycle_id, origin_event_id, "
                "wa_turn_id, contact_key, client_id, phase, last_proven_status, "
                "created_at, updated_at) "
                "VALUES ('fx_one', ?, ?, ?, 12800, ?, ?, 1000.0, 1000.0)",
                (origin, ORIGIN_TURN, self.contact_key, phase, expected),
            )
        )
        self.runtime.write(
            lambda conn: conn.execute(
                "INSERT INTO lifecycle_effects (effect_id, lifecycle_id, "
                "expected_status, target_status, cause, state, created_at, updated_at) "
                "VALUES ('fx_effect', 'fx_one', ?, ?, 'first_t1_send_success', "
                "'pending', 1000.0, 1000.0)",
                (expected, target),
            )
        )

    def effect(self) -> sqlite3.Row:
        return self.runtime.read(
            lambda conn: conn.execute("SELECT * FROM lifecycle_effects").fetchone()
        )

    def lifecycle(self) -> sqlite3.Row:
        return self.runtime.read(
            lambda conn: conn.execute("SELECT * FROM lead_lifecycles").fetchone()
        )

    # ------------------------------------------------------------------

    def test_claim_carries_the_proof_the_writer_needs(self) -> None:
        claim = self.service.claim()

        self.assertIsNotNone(claim)
        self.assertEqual(claim.effect_id, "fx_effect")
        self.assertEqual(claim.client_id, 12800)
        self.assertEqual(claim.expected_status, SEM_ATENDIMENTO)
        self.assertEqual(claim.target_status, NAO_RESPONDEU)
        self.assertEqual(claim.expected_phone_e164, PHONE)
        self.assertEqual(claim.mode, "shadow")
        self.assertEqual(len(claim.lease_token), 32)

    def test_a_second_writer_gets_nothing_while_the_lease_holds(self) -> None:
        self.assertIsNotNone(self.service.claim())

        self.assertIsNone(self.service.claim())

    def test_an_expired_lease_is_reclaimable(self) -> None:
        first = self.service.claim()
        self.now += 121.0

        second = self.service.claim()

        self.assertIsNotNone(second)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertEqual(self.effect()["attempts"], 2)

    def test_an_unresolvable_contact_is_never_handed_over(self) -> None:
        """Without the phone the writer cannot tell whose record it is looking at."""
        self.resolvable = False

        self.assertIsNone(self.service.claim())
        self.assertEqual(self.effect()["state"], "pending")

    def test_a_terminal_lifecycle_is_never_claimed(self) -> None:
        self.runtime.write(
            lambda conn: conn.execute("UPDATE lead_lifecycles SET phase = 'terminal'")
        )
        self.assertIsNone(self.service.claim())

    def test_an_unauthorised_transition_is_never_handed_over(self) -> None:
        self.runtime.write(
            lambda conn: conn.execute(
                "UPDATE lifecycle_effects SET expected_status = ?, target_status = ?",
                (EM_ATENDIMENTO, SEM_ATENDIMENTO),
            )
        )
        self.assertIsNone(self.service.claim())

    def test_the_raw_lease_token_is_never_stored(self) -> None:
        claim = self.service.claim()

        stored = json.dumps(dict(self.effect()), default=str)
        self.assertNotIn(claim.lease_token, stored)
        self.assertNotIn(PHONE, stored)

    # ------------------------------------------------------------------

    def test_applied_updates_the_proven_status(self) -> None:
        claim = self.service.claim()

        self.assertTrue(
            self.service.report(claim.effect_id, claim.lease_token, "applied")
        )

        self.assertEqual(self.effect()["state"], "applied")
        self.assertEqual(self.lifecycle()["last_proven_status"], NAO_RESPONDEU)

    def test_already_applied_also_updates_the_baseline(self) -> None:
        claim = self.service.claim()

        self.service.report(claim.effect_id, claim.lease_token, "already_applied")

        self.assertEqual(self.lifecycle()["last_proven_status"], NAO_RESPONDEU)

    def test_conflict_leaves_the_baseline_untouched(self) -> None:
        claim = self.service.claim()

        self.service.report(claim.effect_id, claim.lease_token, "conflict")

        self.assertEqual(self.effect()["state"], "conflict")
        self.assertEqual(self.lifecycle()["last_proven_status"], SEM_ATENDIMENTO)

    def test_a_wrong_lease_is_refused(self) -> None:
        self.service.claim()

        self.assertFalse(self.service.report("fx_effect", "0" * 32, "applied"))
        self.assertEqual(self.effect()["state"], "claimed")

    def test_a_stale_writer_cannot_settle_a_reclaimed_effect(self) -> None:
        """Crash recovery: the old holder must not overwrite the new one."""
        first = self.service.claim()
        self.now += 121.0
        self.service.claim()

        self.assertFalse(
            self.service.report(first.effect_id, first.lease_token, "applied")
        )
        self.assertEqual(self.effect()["state"], "claimed")

    def test_an_expired_lease_cannot_be_reported(self) -> None:
        claim = self.service.claim()
        self.now += 121.0

        self.assertFalse(
            self.service.report(claim.effect_id, claim.lease_token, "applied")
        )

    def test_an_unknown_result_is_refused(self) -> None:
        claim = self.service.claim()

        self.assertFalse(
            self.service.report(claim.effect_id, claim.lease_token, "whatever")
        )
        self.assertEqual(self.effect()["state"], "claimed")

    def test_reporting_an_unclaimed_effect_is_refused(self) -> None:
        self.assertFalse(self.service.report("fx_effect", "0" * 32, "applied"))

    def test_blocked_contacts_are_counted_without_pii(self) -> None:
        self.resolvable = False
        self.assertEqual(self.service.blocked_contact_count(), 1)

        self.resolvable = True
        self.assertEqual(self.service.blocked_contact_count(), 0)

    def test_mode_must_be_one_of_the_three(self) -> None:
        for mode in ("shadow", "dry_run", "write"):
            with self.subTest(mode=mode):
                self.assertEqual(self.build(mode=mode).claim().mode, mode)
                self.runtime.write(
                    lambda conn: conn.execute(
                        "UPDATE lifecycle_effects SET state = 'pending', "
                        "lease_expires_at = NULL"
                    )
                )
        with self.assertRaises(ValueError):
            self.build(mode="enabled")


if __name__ == "__main__":
    unittest.main()
