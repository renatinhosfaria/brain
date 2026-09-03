from __future__ import annotations

import asyncio
import logging
import time
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self

import httpx2
from mcp import types as mcp_types

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.meta_ads_mcp import (
    RemoteMetaAdsMcpClient,
    _OperationBudget,
    _ResponseBudgetTransport,
    _ResponseTooLarge,
)
from brain.meta_ads_models import (
    META_READ_TOOLS,
    MetaAdsError,
    RemoteAd,
    RemoteCampaign,
)

ACCOUNT_ID = "1598606388477916"
FIXTURE_KEY = "test-key-that-must-not-appear-in-errors"


def _settings(**overrides: object) -> BrainSettings:
    values: dict[str, object] = {
        "principals": {
            "default": PrincipalConfig(
                "default",
                "gateway",
                token_digest("gateway-token"),
                frozenset({"conversation_context"}),
            )
        },
        "cursor_secret": b"c" * 32,
        "meta_ads_mcp_api_key": FIXTURE_KEY,
    }
    values.update(overrides)
    return BrainSettings(**values)


def _tools(*names: str) -> mcp_types.ListToolsResult:
    return mcp_types.ListToolsResult(
        tools=[
            mcp_types.Tool(name=name, inputSchema={"type": "object"}) for name in names
        ]
    )


def _result(value: object, *, error: bool = False) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[], structuredContent=value, isError=error)


def _accounts(*ids: str) -> mcp_types.CallToolResult:
    return _result({"data": [{"id": account_id} for account_id in ids]})


class _FakeHttpClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeSession:
    def __init__(
        self,
        *,
        tools: mcp_types.ListToolsResult | None = None,
        responses: dict[str, list[mcp_types.CallToolResult]] | None = None,
        initialize_error: BaseException | None = None,
        pause_seconds: float = 0.0,
    ) -> None:
        self.tools = tools or _tools(*sorted(META_READ_TOOLS))
        self.responses = responses or {}
        self.initialize_error = initialize_error
        self.pause_seconds = pause_seconds
        self.initialize_calls = 0
        self.list_calls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed += 1

    async def initialize(self) -> None:
        self.initialize_calls += 1
        if self.pause_seconds:
            await asyncio.sleep(self.pause_seconds)
        if self.initialize_error is not None:
            raise self.initialize_error

    async def list_tools(self) -> mcp_types.ListToolsResult:
        self.list_calls += 1
        return self.tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        self.calls.append((name, arguments))
        results = self.responses.get(name)
        if not results:
            raise AssertionError("missing fake structured result")
        return results.pop(0)


class _ByteStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class _StaticTransport(httpx2.AsyncBaseTransport):
    def __init__(self, body: list[bytes]) -> None:
        self._body = body

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request, stream=_ByteStream(self._body))

    async def aclose(self) -> None:
        return None


class RemoteMetaAdsMcpClientTests(unittest.TestCase):
    def _client(
        self,
        *sessions: _FakeSession,
        settings: BrainSettings | None = None,
        captured_http: list[dict[str, Any]] | None = None,
    ) -> RemoteMetaAdsMcpClient:
        pending = list(sessions)

        def http_factory(**kwargs: Any) -> _FakeHttpClient:
            if captured_http is not None:
                captured_http.append(kwargs)
            return _FakeHttpClient()

        @asynccontextmanager
        async def session_factory(_http_client: object) -> AsyncIterator[_FakeSession]:
            if not pending:
                raise AssertionError("unexpected session creation")
            session = pending.pop(0)
            async with session:
                yield session

        return RemoteMetaAdsMcpClient(
            settings or _settings(),
            _http_client_factory=http_factory,
            _session_factory=session_factory,
        )

    @staticmethod
    def _ready_responses(
        *extra: tuple[str, mcp_types.CallToolResult],
    ) -> dict[str, list[mcp_types.CallToolResult]]:
        responses = {"meta_list_ad_accounts": [_accounts(f"act_{ACCOUNT_ID}")]}
        for name, result in extra:
            responses.setdefault(name, []).append(result)
        return responses

    def test_probe_builds_an_exact_bearer_http_client_without_redirects(self) -> None:
        captured: list[dict[str, Any]] = []
        session = _FakeSession(responses=self._ready_responses())
        client = self._client(session, captured_http=captured)
        self.addCleanup(client.close)

        client.probe()

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0]["headers"], {"Authorization": f"Bearer {FIXTURE_KEY}"}
        )
        self.assertEqual(captured[0]["timeout"], 4.0)
        self.assertFalse(captured[0]["follow_redirects"])
        self.assertFalse(captured[0]["trust_env"])
        self.assertEqual(
            captured[0]["base_url"], "https://mcp-facebook-ads.famachat.com.br/mcp"
        )

    def test_streamable_http_debug_records_are_suppressed_for_payload_safety(
        self,
    ) -> None:
        logger = logging.getLogger("mcp.client.streamable_http")
        seen: list[str] = []

        class Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        handler = Handler()
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            logger.debug("source_id=101 remote_payload=untrusted")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        self.assertEqual(seen, [])

    def test_initialized_session_is_reused_for_ad_and_campaign_calls(self) -> None:
        ad = _result(
            {
                "id": "101",
                "name": "Ad name",
                "campaign_id": "202",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        )
        campaign = _result(
            {
                "id": "202",
                "name": "Campaign",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        )
        session = _FakeSession(
            responses=self._ready_responses(
                ("meta_get_ad", ad), ("meta_get_campaign", campaign)
            )
        )
        client = self._client(session)
        self.addCleanup(client.close)

        self.assertEqual(
            client.get_ad("101"), RemoteAd("101", "Ad name", "202", "ACTIVE", "ACTIVE")
        )
        self.assertEqual(
            client.get_campaign("202"),
            RemoteCampaign("202", "Campaign", "ACTIVE", "ACTIVE"),
        )

        self.assertEqual(session.initialize_calls, 1)
        self.assertEqual(session.list_calls, 1)
        self.assertEqual(
            session.calls,
            [
                ("meta_list_ad_accounts", {}),
                ("meta_get_ad", {"ad_id": "101"}),
                ("meta_get_campaign", {"campaign_id": "202"}),
            ],
        )

    def test_transport_failure_discards_session_and_later_call_recreates_it(
        self,
    ) -> None:
        request = httpx2.Request("POST", "https://mcp-facebook-ads.famachat.com.br/mcp")
        failed = _FakeSession(
            initialize_error=httpx2.ConnectError("network", request=request)
        )
        succeeding = _FakeSession(responses=self._ready_responses())
        client = self._client(failed, succeeding)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_server_unavailable$"):
            client.probe()
        client.probe()

        self.assertEqual(failed.closed, 1)
        self.assertEqual(succeeding.initialize_calls, 1)

    def test_expired_deadline_maps_to_timeout_without_calling_the_remote_tool(
        self,
    ) -> None:
        session = _FakeSession(responses=self._ready_responses(), pause_seconds=0.1)
        client = self._client(session)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_timeout$"):
            client.probe(deadline=time.monotonic() + 0.001)

        self.assertEqual(session.calls, [])

    def test_response_stream_budget_rejects_oversized_body_before_it_is_consumed(
        self,
    ) -> None:
        budget = _OperationBudget(3)
        transport = _ResponseBudgetTransport(
            _StaticTransport([b"ab", b"cd"]), lambda: budget
        )

        async def request() -> None:
            async with (
                httpx2.AsyncClient(transport=transport) as http_client,
                http_client.stream("GET", "https://example.invalid") as response,
            ):
                await response.aread()

        with self.assertRaisesRegex(_ResponseTooLarge, "byte limit"):
            asyncio.run(request())
        self.assertTrue(budget.invalid_response)

    def test_probe_rejects_a_missing_required_tool_before_the_account_call(
        self,
    ) -> None:
        session = _FakeSession(
            tools=_tools("meta_list_ad_accounts", "meta_get_ad"),
            responses=self._ready_responses(),
        )
        client = self._client(session)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_required_tool_unavailable$"):
            client.probe()

        self.assertEqual(session.calls, [])

    def test_account_probe_accepts_only_the_single_prefixed_configured_account(
        self,
    ) -> None:
        for accounts in ([], [f"act_{ACCOUNT_ID}", "act_2"], [ACCOUNT_ID]):
            with self.subTest(accounts=accounts):
                session = _FakeSession(
                    responses={"meta_list_ad_accounts": [_accounts(*accounts)]}
                )
                client = self._client(session)
                self.addCleanup(client.close)

                with self.assertRaisesRegex(MetaAdsError, "^meta_account_mismatch$"):
                    client.probe()

    def test_ad_and_campaign_parse_only_structured_results(self) -> None:
        ad = _result(
            {
                "id": "101",
                "name": "Ad",
                "campaign_id": "202",
                "status": "PAUSED",
                "effective_status": "PAUSED",
            }
        )
        campaign = _result(
            {
                "id": "202",
                "name": "Campaign",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        )
        session = _FakeSession(
            responses=self._ready_responses(
                ("meta_get_ad", ad), ("meta_get_campaign", campaign)
            )
        )
        client = self._client(session)
        self.addCleanup(client.close)

        self.assertEqual(client.get_ad("101").status, "PAUSED")
        self.assertEqual(client.get_campaign("202").name, "Campaign")

    def test_malformed_or_remote_error_result_uses_safe_bounded_errors(self) -> None:
        malformed = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text="untrusted response text")]
        )
        bad_session = _FakeSession(
            responses=self._ready_responses(("meta_get_ad", malformed))
        )
        bad_client = self._client(bad_session)
        self.addCleanup(bad_client.close)
        with self.assertRaisesRegex(
            MetaAdsError, "^meta_invalid_response$"
        ) as malformed_error:
            bad_client.get_ad("101")
        self.assertNotIn("untrusted response text", str(malformed_error.exception))

        failed_session = _FakeSession(
            responses=self._ready_responses(("meta_get_ad", _result({}, error=True)))
        )
        failed_client = self._client(failed_session)
        self.addCleanup(failed_client.close)
        with self.assertRaisesRegex(MetaAdsError, "^meta_server_unavailable$"):
            failed_client.get_ad("101")

    def test_unauthorized_response_opens_the_local_authentication_circuit(self) -> None:
        request = httpx2.Request("POST", "https://mcp-facebook-ads.famachat.com.br/mcp")
        response = httpx2.Response(401, request=request)
        unauthorized = _FakeSession(
            initialize_error=httpx2.HTTPStatusError(
                "unauthorized", request=request, response=response
            )
        )
        unconsumed = _FakeSession(responses=self._ready_responses())
        client = self._client(unauthorized, unconsumed)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_auth_unavailable$"):
            client.probe()
        with self.assertRaisesRegex(MetaAdsError, "^meta_auth_unavailable$"):
            client.probe()

        self.assertEqual(unconsumed.initialize_calls, 0)

    def test_public_operations_never_dispatch_a_write_tool_name(self) -> None:
        ad = _result(
            {
                "id": "101",
                "name": "Ad",
                "campaign_id": "202",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        )
        campaign = _result(
            {
                "id": "202",
                "name": "Campaign",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        )
        session = _FakeSession(
            tools=_tools(
                *sorted(META_READ_TOOLS), "meta_create_campaign", "meta_update_ad"
            ),
            responses=self._ready_responses(
                ("meta_get_ad", ad), ("meta_get_campaign", campaign)
            ),
        )
        client = self._client(session)
        self.addCleanup(client.close)

        client.get_ad("101")
        client.get_campaign("202")

        self.assertTrue(
            {name for name, _arguments in session.calls}.issubset(META_READ_TOOLS)
        )
