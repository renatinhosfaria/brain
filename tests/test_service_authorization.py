from __future__ import annotations

import unittest

from brain.authorization import Authorizer, ServiceRequestIdentity
from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.errors import BrainError


class _NoDatabaseAccess:
    def read(self, _operation):
        raise AssertionError("header parsing must not access Hermes databases")


class ServiceAuthorizationTests(unittest.TestCase):
    @staticmethod
    def _principal(name: str, mode: str, token: str, *tools: str) -> PrincipalConfig:
        return PrincipalConfig(name, mode, token_digest(token), frozenset(tools))

    def _authorizer(
        self, gateway_tools: tuple[str, ...] = ("conversation_context", "turn_register")
    ) -> Authorizer:
        settings = BrainSettings(
            principals={
                "default": self._principal(
                    "default", "gateway", "gateway-token", *gateway_tools
                ),
                "observer": self._principal(
                    "observer", "service", "observer-token", "transport_ingest"
                ),
                "writer": self._principal(
                    "writer",
                    "service",
                    "writer-token",
                    "lifecycle_claim",
                    "lifecycle_result",
                ),
                "worker": self._principal(
                    "worker", "worker", "worker-token", "conversation_phone"
                ),
            },
            cursor_secret=b"c" * 32,
        )
        unavailable = _NoDatabaseAccess()
        return Authorizer(settings, unavailable, unavailable)

    @staticmethod
    def _headers(token: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", **extra}

    def assert_denied(self, expected: str, operation) -> None:
        with self.assertRaises(BrainError) as denied:
            operation()
        self.assertEqual(denied.exception.code, expected)

    def test_observer_service_can_only_ingest_transport(self) -> None:
        authorizer = self._authorizer()

        identity = authorizer.parse_service_headers(
            self._headers("observer-token"), "transport_ingest"
        )

        self.assertEqual(identity, ServiceRequestIdentity(principal="observer"))
        self.assert_denied(
            "AUTH_TOOL_DENIED",
            lambda: authorizer.parse_service_headers(
                self._headers("observer-token"), "lifecycle_claim"
            ),
        )

    def test_writer_service_can_claim_and_report_only(self) -> None:
        authorizer = self._authorizer()

        for capability in ("lifecycle_claim", "lifecycle_result"):
            with self.subTest(capability=capability):
                self.assertEqual(
                    authorizer.parse_service_headers(
                        self._headers("writer-token"), capability
                    ),
                    ServiceRequestIdentity(principal="writer"),
                )
        self.assert_denied(
            "AUTH_TOOL_DENIED",
            lambda: authorizer.parse_service_headers(
                self._headers("writer-token"), "transport_ingest"
            ),
        )

    def test_worker_and_gateway_tokens_are_not_service_authority(self) -> None:
        authorizer = self._authorizer()

        for token in ("worker-token", "gateway-token"):
            with self.subTest(token=token):
                self.assert_denied(
                    "AUTH_MODE_MISMATCH",
                    lambda token=token: authorizer.parse_service_headers(
                        self._headers(token), "transport_ingest"
                    ),
                )

    def test_service_token_is_not_gateway_authority(self) -> None:
        authorizer = self._authorizer()

        self.assert_denied(
            "AUTH_MODE_MISMATCH",
            lambda: authorizer.parse_gateway_headers(
                self._headers("observer-token"), "conversation_context"
            ),
        )

    def test_gateway_parser_checks_explicit_capability(self) -> None:
        authorizer = self._authorizer()

        for capability in ("conversation_context", "turn_register"):
            with self.subTest(capability=capability):
                identity = authorizer.parse_gateway_headers(
                    self._headers("gateway-token"), capability
                )
                self.assertEqual(identity.principal, "default")
        self.assert_denied(
            "AUTH_TOOL_DENIED",
            lambda: authorizer.parse_gateway_headers(
                self._headers("gateway-token"), "transport_ingest"
            ),
        )

    def test_gateway_parser_preserves_legacy_phone_default(self) -> None:
        authorizer = self._authorizer(("conversation_phone",))

        identity = authorizer.parse_gateway_headers(self._headers("gateway-token"))

        self.assertEqual(identity.principal, "default")

    def test_invalid_token_remains_fail_closed(self) -> None:
        authorizer = self._authorizer()

        self.assert_denied(
            "AUTH_INVALID_TOKEN",
            lambda: authorizer.parse_service_headers(
                self._headers("invalid-token"), "transport_ingest"
            ),
        )

    def test_worker_headers_do_not_expand_service_authority(self) -> None:
        authorizer = self._authorizer()
        headers = self._headers(
            "observer-token", **{"X-Hermes-Task": "task-a", "X-Hermes-Run": "101"}
        )

        identity = authorizer.parse_service_headers(headers, "transport_ingest")

        self.assertEqual(identity, ServiceRequestIdentity(principal="observer"))
        self.assert_denied(
            "AUTH_TOOL_DENIED",
            lambda: authorizer.parse_service_headers(headers, "lifecycle_claim"),
        )


if __name__ == "__main__":
    unittest.main()
