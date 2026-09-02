from __future__ import annotations

import unittest

from brain.raw_attribution import (
    RawAttributionError,
    RawAttributionLimits,
    assert_raw_matches_normalized,
    canonicalize_raw_attribution,
    decode_canonical_raw_attribution,
)
from brain.transport_models import RuntimeIds


class RawAttributionTests(unittest.TestCase):
    def test_canonicalizes_tagged_binary(self) -> None:
        raw = {
            "sourceId": "source-id",
            "thumbnail": {
                "$type": "bytes",
                "encoding": "base64",
                "data": "AAEC/w==",
            },
        }

        self.assertEqual(
            canonicalize_raw_attribution(
                raw, RawAttributionLimits(4_194_304, 32, 10_000)
            ),
            '{"sourceId":"source-id","thumbnail":'
            '{"$type":"bytes","data":"AAEC/w==","encoding":"base64"}}',
        )

    def test_uses_ecmascript_number_text_at_the_exact_byte_ceiling(self) -> None:
        maximum = 4 * 1024 * 1024
        empty = '{"number":1e-7,"padding":""}'
        padding = "x" * (maximum - len(empty.encode("utf-8")))

        encoded = canonicalize_raw_attribution(
            {"number": 1e-7, "padding": padding},
            RawAttributionLimits(maximum, 32, 10_000),
        )

        self.assertEqual(len(encoded.encode("utf-8")), maximum)
        self.assertTrue(encoded.startswith('{"number":1e-7,'))

    def test_rejects_normalized_hmac_mismatch(self) -> None:
        ids = RuntimeIds(b"t" * 32)
        normalized = {
            "source_id_present": True,
            "source_id_length": 9,
            "source_id_hmac": ids.opaque_hmac("different"),
        }

        with self.assertRaises(RawAttributionError) as caught:
            assert_raw_matches_normalized({"sourceId": "source-id"}, normalized, ids)

        self.assertEqual(caught.exception.code, "raw_normalized_mismatch")

    def test_rejects_noncanonical_encoded_json(self) -> None:
        with self.assertRaises(RawAttributionError) as caught:
            decode_canonical_raw_attribution('{"sourceId": "source-id"}')

        self.assertEqual(caught.exception.code, "raw_canonical")

    def test_rejects_invalid_tag_and_non_finite_number(self) -> None:
        for raw in (
            {"thumbnail": {"$type": "bytes", "encoding": "base64", "data": "AAE"}},
            {"value": float("nan")},
        ):
            with self.subTest(raw=raw), self.assertRaises(RawAttributionError):
                canonicalize_raw_attribution(raw, RawAttributionLimits())

    def test_rejects_integer_valued_unsafe_floats(self) -> None:
        for value in (9007199254740992.0, 1e20):
            with self.subTest(value=value):
                with self.assertRaises(RawAttributionError) as caught:
                    canonicalize_raw_attribution(
                        {"value": value}, RawAttributionLimits()
                    )

                self.assertEqual(caught.exception.code, "raw_type")

    def test_matches_observer_known_fields(self) -> None:
        ids = RuntimeIds(b"t" * 32)
        raw = {
            "sourceType": "ad",
            "sourceApp": "Instagram",
            "sourceId": "source-id",
            "ctwaClid": "clid",
            "sourceUrl": "https://Instagram.com/path",
            "showAdAttribution": True,
            "clickToWhatsappCall": False,
            "containsAutoReply": True,
        }
        normalized = {
            "source_type": "ad",
            "source_app": "Instagram",
            "source_id_present": True,
            "source_id_length": 9,
            "source_id_hmac": ids.opaque_hmac("source-id"),
            "ctwa_clid_present": True,
            "ctwa_clid_length": 4,
            "ctwa_clid_hmac": ids.opaque_hmac("clid"),
            "source_url_hostname": "instagram.com",
            "source_url_length": len("https://Instagram.com/path"),
            "source_url_hmac": ids.opaque_hmac("https://Instagram.com/path"),
            "show_ad_attribution": True,
            "click_to_whatsapp_call": False,
            "contains_auto_reply": True,
        }

        assert_raw_matches_normalized(raw, normalized, ids)

    def test_matches_whatwg_idna_hostname_with_a_valid_port(self) -> None:
        ids = RuntimeIds(b"t" * 32)
        url = "https://bücher.example:8443/path"
        normalized = {
            "source_type": None,
            "source_app": None,
            "source_id_present": False,
            "source_id_length": None,
            "source_id_hmac": None,
            "ctwa_clid_present": False,
            "ctwa_clid_length": None,
            "ctwa_clid_hmac": None,
            "source_url_hostname": "xn--bcher-kva.example",
            "source_url_length": len(url),
            "source_url_hmac": ids.opaque_hmac(url),
            "show_ad_attribution": None,
            "click_to_whatsapp_call": None,
            "contains_auto_reply": None,
        }

        assert_raw_matches_normalized({"sourceUrl": url}, normalized, ids)

    def test_invalid_whatwg_port_produces_no_normalized_url(self) -> None:
        ids = RuntimeIds(b"t" * 32)
        normalized = {
            "source_type": None,
            "source_app": None,
            "source_id_present": False,
            "source_id_length": None,
            "source_id_hmac": None,
            "ctwa_clid_present": False,
            "ctwa_clid_length": None,
            "ctwa_clid_hmac": None,
            "source_url_hostname": None,
            "source_url_length": None,
            "source_url_hmac": None,
            "show_ad_attribution": None,
            "click_to_whatsapp_call": None,
            "contains_auto_reply": None,
        }

        assert_raw_matches_normalized(
            {"sourceUrl": "https://example.test:99999/path"},
            normalized,
            ids,
        )


if __name__ == "__main__":
    unittest.main()
