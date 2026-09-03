import unittest

from brain.meta_ads_models import (
    META_ERROR_CODES,
    META_READ_TOOLS,
    ConfirmedMetaAttribution,
    MetaAdsError,
    ObservedCtwaSource,
    RemoteAd,
    RemoteCampaign,
    normalize_ad_account_id,
    observed_ctwa_source,
)


class MetaAdsModelsTests(unittest.TestCase):
    def test_constants_are_exact_read_only_boundary(self):
        self.assertEqual(META_READ_TOOLS, frozenset({"meta_list_ad_accounts", "meta_get_ad", "meta_get_campaign"}))
        self.assertIn("meta_inactive", META_ERROR_CODES)

    def test_observer_source_preserves_decimal_string_exactly(self):
        result = observed_ctwa_source({"sourceType": "ad", "sourceId": "00042", "ctwaClid": "clid"})
        self.assertEqual(result, ObservedCtwaSource("00042", "clid"))

    def test_observer_source_rejects_noncanonical_or_invalid_shapes(self):
        for raw in (
            None, [], {}, {"sourceType": "campaign", "sourceId": "42"},
            {"sourceType": "ad", "sourceId": 42},
            {"sourceType": "ad", "sourceId": ""},
            {"sourceType": "ad", "sourceId": "9" * 65},
        ):
            self.assertIsNone(observed_ctwa_source(raw))
        self.assertEqual(observed_ctwa_source({"sourceType": "ad", "sourceId": "42", "hash": "x"}).source_id, "42")

    def test_remote_models_validate_ids_text_and_keep_status_fields(self):
        ad = RemoteAd("1", "Ad", "0002", "PAUSED", "ACTIVE")
        campaign = RemoteCampaign("0002", "Campaign", "ACTIVE", "ACTIVE")
        self.assertEqual(ad.effective_status, "ACTIVE")
        self.assertEqual(campaign.campaign_id, "0002")
        for cls, args in (
            (RemoteAd, ("", "Ad", "2", "ACTIVE", "ACTIVE")),
            (RemoteAd, ("1", None, "2", "ACTIVE", "ACTIVE")),
            (RemoteCampaign, ("1", "\n", "ACTIVE", "ACTIVE")),
            (RemoteCampaign, ("1", "é" * 257, "ACTIVE", "ACTIVE")),
        ):
            with self.assertRaises(ValueError):
                cls(*args)

    def test_confirmation_requires_both_effective_statuses_active(self):
        with self.assertRaises(ValueError):
            ConfirmedMetaAttribution("1", "Ad", "2", "Campaign", "ACTIVE", "PAUSED", "ACTIVE", "ACTIVE")
        confirmed = ConfirmedMetaAttribution("1", "Ad", "2", "Campaign", "PAUSED", "ACTIVE", "PAUSED", "ACTIVE")
        self.assertEqual(confirmed.ad_status, "PAUSED")

    def test_error_only_exposes_bounded_code(self):
        error = MetaAdsError("meta_timeout", retry_after_seconds=1.5)
        self.assertEqual(str(error), "meta_timeout")
        self.assertEqual(error.args, ("meta_timeout",))
        with self.assertRaises(ValueError):
            MetaAdsError("remote secret leaked")

    def test_account_normalization_accepts_only_configured_forms(self):
        self.assertEqual(normalize_ad_account_id("1598606388477916"), "act_1598606388477916")
        self.assertEqual(normalize_ad_account_id("act_1598606388477916"), "act_1598606388477916")
        with self.assertRaises(ValueError):
            normalize_ad_account_id("act_1")


if __name__ == "__main__":
    unittest.main()
