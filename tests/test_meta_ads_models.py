import math
import unittest

from brain.meta_ads_models import (
    META_ERROR_CODES,
    MetaAdRecord,
    MetaAdsError,
    MetaAttributionView,
    ObservedAttribution,
    canonical_account_id,
    confirmed_payload,
    eligible_source,
    pending_payload,
)


class MetaAdsModelTests(unittest.TestCase):
    def test_canonical_account_is_pinned_and_normalized(self):
        self.assertEqual(
            canonical_account_id("act_1598606388477916"), "1598606388477916"
        )
        self.assertEqual(canonical_account_id("1598606388477916"), "1598606388477916")
        for foreign in ("act_1", "1598606388477917", " act_1598606388477916"):
            with self.subTest(foreign=foreign), self.assertRaises(ValueError):
                canonical_account_id(foreign)

    def test_only_decimal_ad_source_is_eligible(self):
        observed = eligible_source(
            {
                "sourceType": "ad",
                "sourceId": "120200000000001",
                "ctwaClid": "click-evidence",
            }
        )
        self.assertEqual(
            observed, ObservedAttribution("120200000000001", "click-evidence")
        )
        self.assertIsNone(eligible_source({"sourceType": "post", "sourceId": "1202"}))
        self.assertIsNone(eligible_source({"sourceType": "ad", "sourceId": "12_02"}))

    def test_source_id_boundaries_and_names(self):
        for length in (1, 64):
            self.assertIsNotNone(
                eligible_source({"sourceType": "ad", "sourceId": "1" * length})
            )
        for source_id in ("", "1" * 65, "１２３"):
            self.assertIsNone(
                eligible_source({"sourceType": "ad", "sourceId": source_id})
            )
        with self.assertRaises(ValueError):
            MetaAdRecord(
                "1598606388477916",
                "1",
                "x" * 513,
                None,
                None,
                None,
                None,
                None,
                "2",
                "c",
                None,
                None,
                None,
                True,
                1.0,
            )

    def test_meta_error_is_bounded_and_does_not_expose_fixture(self):
        error = MetaAdsError("meta_timeout", 2.5)
        self.assertEqual(error.code, "meta_timeout")
        self.assertEqual(error.retry_after_seconds, 2.5)
        self.assertNotIn("fixture", repr(error))
        self.assertEqual(len(META_ERROR_CODES), 9)
        for value in ("unknown", "fixture-token"):
            with self.assertRaises(ValueError):
                MetaAdsError(value)
        for value in (math.nan, math.inf, -1):
            with self.assertRaises(ValueError):
                MetaAdsError("meta_timeout", value)

    def test_payload_shapes(self):
        observed = ObservedAttribution("120200000000001", "clid")
        record = MetaAdRecord(
            "1598606388477916",
            "120200000000001",
            "Ad",
            "PAUSED",
            "ACTIVE",
            "3",
            "Set",
            "ACTIVE",
            "4",
            "Campaign",
            "ACTIVE",
            "5",
            "Creative",
            True,
            101.0,
        )
        confirmed = confirmed_payload(record, observed, 102.0)
        self.assertEqual(confirmed["account_id"], "act_1598606388477916")
        self.assertEqual(confirmed["matched_by"], "source_id_exact")
        self.assertEqual(confirmed["ad"]["status"], "ACTIVE")
        self.assertEqual(confirmed["metadata_fetched_at"], "1970-01-01T00:01:41Z")
        pending = pending_payload(observed, None, True, None)
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["last_attempt_at"])

    def test_optional_hierarchy_fields_are_paired_and_complete_flag_is_honest(self):
        args = [
            "1598606388477916",
            "1",
            "Ad",
            None,
            None,
            None,
            None,
            None,
            "2",
            "Campaign",
            None,
            None,
            None,
            True,
            1.0,
        ]
        with self.assertRaises(ValueError):
            MetaAdRecord(*args[:5], "3", None, None, *args[8:])
        with self.assertRaises(ValueError):
            MetaAdRecord(*args[:11], "4", None, True, 1.0)
        with self.assertRaises(ValueError):
            MetaAdRecord(*args[:13], True, 1.0)

    def test_view_validates_types_and_status_timestamp_pairings(self):
        observed = ObservedAttribution("1", None)
        with self.assertRaises(TypeError):
            MetaAttributionView(
                "event", "pending", "pending", None, None, None, None, True
            )
        with self.assertRaises(ValueError):
            MetaAttributionView(
                "event", observed, "pending", None, 1.0, None, None, True
            )
        with self.assertRaises(ValueError):
            MetaAttributionView(
                "event", observed, "confirmed", None, 1.0, 1.0, None, True
            )


if __name__ == "__main__":
    unittest.main()
