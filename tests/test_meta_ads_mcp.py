from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Self

import anyio
import httpx2
from mcp import types as mcp_types

from brain.config import BrainSettings, PrincipalConfig, token_digest
from brain.meta_ads_mcp import (
    READ_TOOLS,
    MetaAdsMcpClient,
    _ResponseLimitTransport,
)
from brain.meta_ads_models import MetaAdsError

ACCOUNT_ID = "1598606388477916"
FIXTURE_TOKEN = "fixture-token-must-not-leak"


def _tool(
    name: str,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        inputSchema=input_schema or {"type": "object", "properties": {}},
        outputSchema=output_schema
        or {
            "type": "object",
            "properties": {"data": {"type": "array", "items": {"type": "object"}}},
            "required": ["data"],
        },
    )


def _tools(
    *,
    account_schema: dict[str, Any] | None = None,
    entity_schema: dict[str, Any] | None = None,
    entity_output: dict[str, Any] | None = None,
    include_accounts: bool = True,
    include_entities: bool = True,
    include_field_context: bool = False,
) -> list[mcp_types.Tool]:
    tools: list[mcp_types.Tool] = []
    if include_accounts:
        tools.append(_tool("ads_get_ad_accounts", input_schema=account_schema))
    if include_entities:
        tools.append(
            _tool(
                "ads_get_ad_entities",
                input_schema=entity_schema
                or {
                    "type": "object",
                    "properties": {
                        "ad_account_id": {"type": "string"},
                        "entity_type": {"type": "string"},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                        "after": {"type": "string"},
                    },
                    "required": ["ad_account_id", "entity_type"],
                },
                output_schema=entity_output,
            )
        )
    if include_field_context:
        tools.append(
            _tool(
                "ads_get_field_context",
                input_schema={
                    "type": "object",
                    "properties": {
                        "entity_type": {"type": "string"},
                        "field": {"type": "string"},
                    },
                },
            )
        )
    return tools


def _result(
    data: dict[str, Any], *, is_error: bool = False
) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[], structuredContent=data, isError=is_error
    )


class _FakeSession:
    def __init__(
        self,
        tools: list[mcp_types.Tool],
        responses: dict[str, list[mcp_types.CallToolResult]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.tools = tools
        self.responses = responses or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def initialize(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_tools(self) -> mcp_types.ListToolsResult:
        if self.error is not None:
            raise self.error
        return mcp_types.ListToolsResult(tools=self.tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        values = self.responses.get(name)
        if not values:
            raise AssertionError("missing synthetic response")
        return values.pop(0)


def _factory(session: _FakeSession) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[_FakeSession]:
        async with session:
            yield session

    return factory


class _ByteStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class _StaticTransport(httpx2.AsyncBaseTransport):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request, stream=_ByteStream([self.body]))

    async def aclose(self) -> None:
        return None


class _SequenceTransport(httpx2.AsyncBaseTransport):
    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = bodies

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            request=request,
            stream=_ByteStream([self.bodies.pop(0)]),
        )

    async def aclose(self) -> None:
        return None


class MetaAdsMcpClientTests(unittest.TestCase):
    def _settings(self, **overrides: object) -> BrainSettings:
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
            "meta_attribution_enabled": True,
            "meta_ad_account_id": f"act_{ACCOUNT_ID}",
            "meta_ads_mcp_access_token": FIXTURE_TOKEN,
        }
        values.update(overrides)
        return BrainSettings(**values)

    def _client(self, session: _FakeSession, **settings: object) -> MetaAdsMcpClient:
        return MetaAdsMcpClient(
            self._settings(**settings), session_factory=_factory(session)
        )

    def _probe_session(
        self,
        tools: list[mcp_types.Tool] | None = None,
        accounts: list[dict[str, Any]] | None = None,
    ) -> _FakeSession:
        return _FakeSession(
            tools or _tools(),
            {
                "ads_get_ad_accounts": [
                    _result({"data": accounts or [{"id": f"act_{ACCOUNT_ID}"}]})
                ]
            },
        )

    def test_probe_discovers_only_required_read_capabilities_for_the_pinned_account(
        self,
    ) -> None:
        session = self._probe_session()

        capabilities = self._client(session).probe()

        self.assertEqual(capabilities.account_id, ACCOUNT_ID)
        self.assertEqual(
            capabilities.required_tools,
            frozenset({"ads_get_ad_accounts", "ads_get_ad_entities"}),
        )
        self.assertEqual(capabilities.account_argument, "ad_account_id")
        self.assertEqual(capabilities.entity_selector_argument, "entity_type")
        self.assertEqual(capabilities.result_array_path, ("data",))

    def test_probe_rejects_missing_required_tool_without_leaking_fixture_token(
        self,
    ) -> None:
        session = self._probe_session(tools=_tools(include_entities=False))

        with self.assertRaisesRegex(
            MetaAdsError, "^meta_required_tool_unavailable$"
        ) as raised:
            self._client(session).probe()

        self.assertNotIn(FIXTURE_TOKEN, str(raised.exception))
        self.assertEqual(session.calls, [])

    def test_probe_rejects_incompatible_entity_schema_before_account_call(self) -> None:
        session = self._probe_session(
            tools=_tools(
                entity_schema={
                    "type": "object",
                    "properties": {"account": {"type": "string"}},
                }
            )
        )

        with self.assertRaisesRegex(MetaAdsError, "^meta_required_tool_unavailable$"):
            self._client(session).probe()

        self.assertEqual(session.calls, [])

    def test_probe_rejects_missing_structured_entity_array_before_account_call(
        self,
    ) -> None:
        session = self._probe_session(
            tools=_tools(
                entity_output={
                    "type": "object",
                    "properties": {"items": {"type": "array"}},
                }
            )
        )

        with self.assertRaisesRegex(MetaAdsError, "^meta_required_tool_unavailable$"):
            self._client(session).probe()

        self.assertEqual(session.calls, [])

    def test_probe_rejects_zero_or_duplicate_configured_accounts(self) -> None:
        for accounts in (
            [{"id": "act_9999999999999999"}],
            [{"id": f"act_{ACCOUNT_ID}"}, {"id": ACCOUNT_ID}],
        ):
            with self.subTest(accounts=accounts):
                session = self._probe_session(accounts=accounts)
                with self.assertRaisesRegex(MetaAdsError, "^meta_account_mismatch$"):
                    self._client(session).probe()

    def test_non_read_tool_fails_locally_before_fake_session_call(self) -> None:
        session = self._probe_session()
        client = self._client(session)

        with self.assertRaisesRegex(MetaAdsError, "^meta_required_tool_unavailable$"):
            anyio.run(client._call_tool, session, "ads_create_campaign", {})

        self.assertEqual(session.calls, [])
        self.assertIn("ads_get_ad_entities", READ_TOOLS)

    def test_get_ad_returns_only_the_exact_same_account_record(self) -> None:
        source_id = "120200000000001"
        result = {
            "data": [
                {"id": "120200000000000", "account_id": ACCOUNT_ID},
                {
                    "id": source_id,
                    "account_id": ACCOUNT_ID,
                    "name": "September lead ad",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "adset": {
                        "id": "1203001",
                        "name": "Prospecting",
                        "status": "ACTIVE",
                    },
                    "campaign": {
                        "id": "1204001",
                        "name": "September",
                        "status": "ACTIVE",
                    },
                    "creative": {"id": "1205001", "name": "Image A"},
                },
            ]
        }
        session = self._probe_session()
        session.responses["ads_get_ad_entities"] = [_result(result)]
        client = self._client(session)

        record = client.get_ad(source_id, now=123.5)

        self.assertEqual(record.ad_id, source_id)
        self.assertEqual(record.campaign_id, "1204001")
        self.assertEqual(record.fetched_at, 123.5)
        entity_call = session.calls[-1]
        self.assertEqual(entity_call[0], "ads_get_ad_entities")
        self.assertEqual(entity_call[1]["ad_account_id"], f"act_{ACCOUNT_ID}")
        self.assertEqual(entity_call[1]["entity_type"], "ad")
        self.assertEqual(entity_call[1]["limit"], 500)

    def test_get_ad_uses_an_exact_id_argument_only_when_the_schema_declares_it(
        self,
    ) -> None:
        source_id = "120200000000001"
        schema = {
            "type": "object",
            "properties": {
                "ad_account_id": {"type": "string"},
                "entity_type": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "after": {"type": "string"},
                "id": {"type": "string"},
            },
        }
        session = self._probe_session(
            tools=_tools(entity_schema=schema, include_field_context=True)
        )
        session.responses["ads_get_field_context"] = [
            _result(
                {"data": [{"entity_type": "ad", "field": "id", "filterable": True}]}
            )
        ]
        session.responses["ads_get_ad_entities"] = [
            _result(
                {
                    "data": [
                        {
                            "id": source_id,
                            "account_id": ACCOUNT_ID,
                            "name": "Fixture ad",
                            "campaign": {"id": "1204001", "name": "Fixture campaign"},
                        }
                    ]
                }
            )
        ]

        self._client(session).get_ad(source_id, now=1.0)

        self.assertEqual(session.calls[-1][1]["id"], source_id)

    def test_get_ad_falls_back_to_local_exact_comparison_when_field_context_is_unproven(
        self,
    ) -> None:
        source_id = "120200000000001"
        schema = {
            "type": "object",
            "properties": {
                "ad_account_id": {"type": "string"},
                "entity_type": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "after": {"type": "string"},
                "id": {"type": "string"},
            },
        }
        session = self._probe_session(
            tools=_tools(entity_schema=schema, include_field_context=True)
        )
        session.responses["ads_get_field_context"] = [
            _result(
                {"data": [{"entity_type": "ad", "field": "id", "filterable": False}]}
            )
        ]
        session.responses["ads_get_ad_entities"] = [
            _result(
                {
                    "data": [
                        {
                            "id": source_id,
                            "account_id": ACCOUNT_ID,
                            "name": "Fixture ad",
                            "campaign": {"id": "1204001", "name": "Fixture campaign"},
                        }
                    ]
                }
            )
        ]

        self._client(session).get_ad(source_id, now=1.0)

        self.assertNotIn("id", session.calls[-1][1])

    def test_probe_treats_malformed_field_context_as_unproven_not_as_text_evidence(
        self,
    ) -> None:
        schema = {
            "type": "object",
            "properties": {
                "ad_account_id": {"type": "string"},
                "entity_type": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "after": {"type": "string"},
                "id": {"type": "string"},
            },
        }
        session = self._probe_session(
            tools=_tools(entity_schema=schema, include_field_context=True)
        )
        session.responses["ads_get_field_context"] = [
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="id is filterable")]
            )
        ]

        capabilities = self._client(session).probe()

        self.assertFalse(capabilities.exact_id_filter_supported)
        self.assertEqual(session.calls[-1][0], "ads_get_field_context")

    def test_probe_rejects_schema_that_cannot_support_bounded_requests(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "ad_account_id": {"type": "string"},
                "entity_type": {"type": "string"},
                "limit": {"type": "integer"},
                "after": {"type": "string"},
            },
        }
        session = self._probe_session(tools=_tools(entity_schema=schema))

        with self.assertRaisesRegex(MetaAdsError, "^meta_required_tool_unavailable$"):
            self._client(session).probe()

        self.assertEqual(session.calls, [])

    def test_get_ad_rejects_text_result_without_exposing_untrusted_content(
        self,
    ) -> None:
        session = self._probe_session()
        session.responses["ads_get_ad_entities"] = [
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="September lead ad")]
            )
        ]

        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$") as raised:
            self._client(session).get_ad("120200000000001", now=1.0)

        self.assertNotIn("September lead ad", str(raised.exception))

    def test_get_ad_rejects_foreign_duplicate_and_incomplete_exact_records(
        self,
    ) -> None:
        source_id = "120200000000001"
        complete = {
            "id": source_id,
            "account_id": ACCOUNT_ID,
            "name": "fixture-ad-name",
            "campaign": {"id": "1204001", "name": "fixture-campaign-name"},
        }
        cases = (
            ([{**complete, "account_id": "1598606388477917"}], "meta_account_mismatch"),
            ([complete, complete], "meta_invalid_response"),
            ([{**complete, "name": ""}], "meta_incomplete_result"),
            (
                [{**complete, "campaign": {"id": "1204001", "name": "x\x00"}}],
                "meta_incomplete_result",
            ),
        )
        for data, code in cases:
            with self.subTest(code=code):
                session = self._probe_session()
                session.responses["ads_get_ad_entities"] = [_result({"data": data})]
                with self.assertRaisesRegex(MetaAdsError, f"^{code}$") as raised:
                    self._client(session).get_ad(source_id, now=1.0)
                self.assertNotIn(FIXTURE_TOKEN, str(raised.exception))
                self.assertNotIn("fixture-ad-name", str(raised.exception))

    def test_get_ad_detects_repeated_cursor_and_bounds_pages(self) -> None:
        source_id = "120200000000001"
        session = self._probe_session()
        session.responses["ads_get_ad_entities"] = [
            _result({"data": [], "paging": {"cursors": {"after": "cursor-one"}}}),
            _result({"data": [], "paging": {"cursors": {"after": "cursor-one"}}}),
        ]

        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$"):
            self._client(session).get_ad(source_id, now=1.0)

        self.assertEqual(len(session.calls), 3)  # accounts plus two entity pages

    def test_get_ad_rejects_page_count_above_the_hard_limit(self) -> None:
        session = self._probe_session()
        session.responses["ads_get_ad_entities"] = [
            _result(
                {
                    "data": [],
                    "paging": {"cursors": {"after": f"cursor-{index}"}},
                }
            )
            for index in range(100)
        ]

        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$"):
            self._client(session).get_ad("120200000000001", now=1.0)

        self.assertEqual(len(session.calls), 101)  # accounts plus the 100-page cap

    def test_get_ad_rejects_mcp_error_results_without_parsing_their_text(self) -> None:
        session = self._probe_session()
        session.responses["ads_get_ad_entities"] = [
            _result({"data": []}, is_error=True)
        ]

        with self.assertRaisesRegex(MetaAdsError, "^meta_server_unavailable$"):
            self._client(session).get_ad("120200000000001", now=1.0)

    def test_list_ads_rejects_more_than_500_entities_in_a_page(self) -> None:
        session = self._probe_session()
        session.responses["ads_get_ad_entities"] = [_result({"data": [{}] * 501})]

        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$"):
            self._client(session).list_ads(now=1.0, full=False)

    def test_maps_http_timeout_rate_auth_and_server_errors_without_disclosure(
        self,
    ) -> None:
        response = httpx2.Response(429, headers={"Retry-After": "2.5"})
        cases: tuple[BaseException, str, float | None] = (
            (httpx2.TimeoutException("fixture-timeout"), "meta_timeout", None),
            (
                httpx2.HTTPStatusError(
                    "fixture-rate",
                    request=httpx2.Request("GET", "https://example.invalid"),
                    response=response,
                ),
                "meta_rate_limited",
                2.5,
            ),
            (
                httpx2.HTTPStatusError(
                    "fixture-auth",
                    request=httpx2.Request("GET", "https://example.invalid"),
                    response=httpx2.Response(401),
                ),
                "meta_auth_unavailable",
                None,
            ),
            (
                httpx2.HTTPStatusError(
                    "fixture-server",
                    request=httpx2.Request("GET", "https://example.invalid"),
                    response=httpx2.Response(503),
                ),
                "meta_server_unavailable",
                None,
            ),
        )
        for error, code, delay in cases:
            with self.subTest(code=code):
                session = self._probe_session()
                session.error = error
                with self.assertRaisesRegex(MetaAdsError, f"^{code}$") as raised:
                    self._client(session).probe()
                self.assertEqual(raised.exception.retry_after_seconds, delay)
                self.assertNotIn(FIXTURE_TOKEN, str(raised.exception))
                self.assertNotIn("fixture-", str(raised.exception))

    def test_response_limit_allows_exactly_eight_mebibytes_and_rejects_one_byte_more(
        self,
    ) -> None:
        limit = 8 * 1024 * 1024

        async def read(body: bytes) -> bytes:
            async with httpx2.AsyncClient(
                transport=_ResponseLimitTransport(_StaticTransport(body), limit)
            ) as http:
                return await (await http.get("https://example.invalid")).aread()

        self.assertEqual(len(anyio.run(read, b"x" * limit)), limit)
        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$"):
            anyio.run(read, b"x" * (limit + 1))

    def test_response_limit_counts_multiple_http_responses_in_one_operation(
        self,
    ) -> None:
        limit = 8 * 1024 * 1024

        async def read_two() -> None:
            transport = _ResponseLimitTransport(
                _SequenceTransport([b"x" * (limit // 2 + 1), b"y" * (limit // 2)]),
                limit,
            )
            async with httpx2.AsyncClient(transport=transport) as http:
                await (await http.get("https://example.invalid/one")).aread()
                await (await http.get("https://example.invalid/two")).aread()

        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$"):
            anyio.run(read_two)

    def test_response_limit_never_exceeds_the_fixed_eight_mebibyte_contract(
        self,
    ) -> None:
        fixed_limit = 8 * 1024 * 1024

        async def read() -> bytes:
            async with httpx2.AsyncClient(
                transport=_ResponseLimitTransport(
                    _StaticTransport(b"x" * (fixed_limit + 1)),
                    32 * 1024 * 1024,
                )
            ) as http:
                return await (await http.get("https://example.invalid")).aread()

        with self.assertRaisesRegex(MetaAdsError, "^meta_invalid_response$"):
            anyio.run(read)


if __name__ == "__main__":
    unittest.main()
