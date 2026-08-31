from __future__ import annotations

import unittest

from brain.famachat_client import (
    FamaChatClient,
    FamaChatUnavailable,
    same_phone,
)

# Observed on 2026-08-31: Brain resolves the mobile with the ninth digit, the
# CRM stores the older ten-digit national form. Without normalisation the
# writer would refuse every effect.
BRAIN_PHONE = "5534999772714"
FAMACHAT_PHONE = "553499772714"

CLIENT_BODY = {
    "id": 12800,
    "fullName": "Lead WhatsApp 2714",
    "phone": FAMACHAT_PHONE,
    "brokerId": 35,
    "status": "Sem Atendimento",
    "source": "Facebook Ads",
}


class PhoneMatchingTests(unittest.TestCase):
    def test_the_ninth_digit_difference_still_matches(self) -> None:
        self.assertTrue(same_phone(BRAIN_PHONE, FAMACHAT_PHONE))
        self.assertTrue(same_phone(FAMACHAT_PHONE, BRAIN_PHONE))

    def test_punctuation_and_spacing_are_ignored(self) -> None:
        self.assertTrue(same_phone(BRAIN_PHONE, "+55 (34) 9977-2714"))
        self.assertTrue(same_phone(BRAIN_PHONE, " 34 99977-2714 "))

    def test_the_country_prefix_is_optional_on_either_side(self) -> None:
        self.assertTrue(same_phone(BRAIN_PHONE, "34999772714"))
        self.assertTrue(same_phone("34999772714", FAMACHAT_PHONE))

    def test_different_numbers_never_match(self) -> None:
        for other in (
            "5534999772715",  # last digit
            "5534999772",  # truncated
            "5511999772714",  # another area code
            "",
            None,
        ):
            with self.subTest(other=other):
                self.assertFalse(same_phone(BRAIN_PHONE, other))

    def test_a_landline_is_not_stretched_into_a_mobile(self) -> None:
        """Dropping a ninth digit only applies to an eleven-digit national form."""
        self.assertFalse(same_phone("553433334444", "5534933334444"))


class FamaChatClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[dict] = []

    def transport(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, arguments))
        if not self.responses:
            raise AssertionError(f"unexpected call to {tool}")
        return self.responses.pop(0)

    def client(self) -> FamaChatClient:
        return FamaChatClient(self.transport)

    def test_get_client_parses_the_real_envelope(self) -> None:
        self.responses = [{"status": 200, "statusText": "OK", "body": CLIENT_BODY}]

        record = self.client().get_client(12800)

        self.assertEqual(self.calls[0][0], "fc_get_clientes_by_id")
        self.assertEqual(self.calls[0][1], {"id": 12800})
        self.assertEqual(record.client_id, 12800)
        self.assertEqual(record.broker_id, 35)
        self.assertEqual(record.status, "Sem Atendimento")
        self.assertEqual(record.source, "Facebook Ads")
        self.assertEqual(record.phone, FAMACHAT_PHONE)

    def test_a_missing_client_is_absent_not_an_error(self) -> None:
        self.responses = [{"status": 404, "body": {"error": "not found"}}]

        self.assertIsNone(self.client().get_client(999999))

    def test_a_server_error_is_unavailable(self) -> None:
        for status in (500, 502, 503):
            with self.subTest(status=status):
                self.responses = [{"status": status, "body": {}}]
                with self.assertRaises(FamaChatUnavailable):
                    self.client().get_client(12800)

    def test_an_unauthorised_response_is_unavailable(self) -> None:
        self.responses = [{"status": 401, "body": {"message": "Token inválido"}}]

        with self.assertRaises(FamaChatUnavailable):
            self.client().get_client(12800)

    def test_a_malformed_body_is_unavailable(self) -> None:
        for body in ("not a dict", [CLIENT_BODY], None):
            with self.subTest(body=body):
                self.responses = [{"status": 200, "body": body}]
                with self.assertRaises(FamaChatUnavailable):
                    self.client().get_client(12800)

    def test_a_record_missing_required_fields_is_unavailable(self) -> None:
        for missing in ("id", "status", "brokerId"):
            with self.subTest(missing=missing):
                body = {
                    key: value for key, value in CLIENT_BODY.items() if key != missing
                }
                self.responses = [{"status": 200, "body": body}]
                with self.assertRaises(FamaChatUnavailable):
                    self.client().get_client(12800)

    def test_a_truncated_response_is_unavailable(self) -> None:
        """A trimmed record could hide the very field being validated."""
        self.responses = [{"status": 200, "body": CLIENT_BODY, "truncated": True}]

        with self.assertRaises(FamaChatUnavailable):
            self.client().get_client(12800)

    def test_the_client_never_calls_a_mutating_tool(self) -> None:
        self.responses = [{"status": 200, "body": CLIENT_BODY}]
        self.client().get_client(12800)

        for tool, _ in self.calls:
            self.assertTrue(tool.startswith("fc_get_"))


if __name__ == "__main__":
    unittest.main()
