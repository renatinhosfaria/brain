from __future__ import annotations

import asyncio
import gzip
import json
import logging
import threading
import time
import unittest
import zlib
from collections.abc import AsyncIterator, Callable
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
        started: threading.Event | None = None,
        teardown_error: BaseException | None = None,
        teardown_hangs: bool = False,
        ignore_cancellation_until: threading.Event | None = None,
    ) -> None:
        self.tools = tools or _tools(*sorted(META_READ_TOOLS))
        self.responses = responses or {}
        self.initialize_error = initialize_error
        self.pause_seconds = pause_seconds
        self.started = started
        self.teardown_error = teardown_error
        self.teardown_hangs = teardown_hangs
        self.ignore_cancellation_until = ignore_cancellation_until
        self.initialize_calls = 0
        self.list_calls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed += 1
        if self.teardown_hangs:
            await asyncio.Event().wait()
        if self.teardown_error is not None:
            raise self.teardown_error

    async def initialize(self) -> None:
        self.initialize_calls += 1
        if self.started is not None:
            self.started.set()
        if self.pause_seconds:
            await asyncio.sleep(self.pause_seconds)
        if self.ignore_cancellation_until is not None:
            while not self.ignore_cancellation_until.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
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
    def __init__(
        self, body: list[bytes], *, headers: dict[str, str] | None = None
    ) -> None:
        self._body = body
        self._headers = headers or {}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers=self._headers,
            request=request,
            stream=_ByteStream(self._body),
        )

    async def aclose(self) -> None:
        return None


class _McpRecorderTransport(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.methods: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.methods.append(request.method)
        if request.method == "POST":
            message = json.loads(request.content)
            if message["method"] == "initialize":
                return httpx2.Response(
                    200,
                    headers={
                        "content-type": "application/json",
                        "mcp-session-id": "fake-session",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "serverInfo": {"name": "fake", "version": "1.0"},
                        },
                    },
                    request=request,
                )
            return httpx2.Response(202, request=request)
        return httpx2.Response(403, request=request)

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
            captured[0]["headers"],
            {
                "Authorization": f"Bearer {FIXTURE_KEY}",
                "Accept-Encoding": "identity",
            },
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

    def test_real_streamable_transport_cleanup_never_sends_delete_or_get(self) -> None:
        recorder = _McpRecorderTransport()
        client = RemoteMetaAdsMcpClient(_settings())
        self.addCleanup(client.close)

        async def initialize_and_close() -> None:
            async with (
                httpx2.AsyncClient(transport=recorder) as http_client,
                client._real_session_factory(http_client) as session,
            ):
                await session.initialize()
                await asyncio.sleep(0)

        asyncio.run(initialize_and_close())

        self.assertIn("POST", recorder.methods)
        self.assertNotIn("DELETE", recorder.methods)
        self.assertNotIn("GET", recorder.methods)

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

    def test_malformed_and_semantic_results_discard_the_session_before_retry(
        self,
    ) -> None:
        malformed = mcp_types.CallToolResult(content=[])
        incomplete = _result(
            {
                "id": "999",
                "name": "Ad",
                "campaign_id": "202",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        )
        for result, expected in (
            (malformed, "meta_invalid_response"),
            (incomplete, "meta_incomplete_result"),
        ):
            with self.subTest(expected=expected):
                failed = _FakeSession(
                    responses=self._ready_responses(("meta_get_ad", result))
                )
                recreated = _FakeSession(responses=self._ready_responses())
                client = self._client(failed, recreated)
                self.addCleanup(client.close)

                with self.assertRaisesRegex(MetaAdsError, f"^{expected}$"):
                    client.get_ad("101")
                client.probe()

                self.assertEqual(failed.closed, 1)
                self.assertEqual(recreated.initialize_calls, 1)

    def test_account_mismatch_discards_the_session_before_retry(self) -> None:
        mismatched = _FakeSession(
            responses={"meta_list_ad_accounts": [_accounts("act_2")]}
        )
        recreated = _FakeSession(responses=self._ready_responses())
        client = self._client(mismatched, recreated)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_account_mismatch$"):
            client.probe()
        client.probe()

        self.assertEqual(mismatched.closed, 1)
        self.assertEqual(recreated.initialize_calls, 1)

    def test_expired_deadline_maps_to_timeout_without_calling_the_remote_tool(
        self,
    ) -> None:
        session = _FakeSession(responses=self._ready_responses(), pause_seconds=0.1)
        client = self._client(session)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_timeout$"):
            client.probe(deadline=time.monotonic() + 0.001)

        self.assertEqual(session.calls, [])

    def test_omitted_deadline_bounds_a_cancellation_resistant_probe(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []
        session = _FakeSession(
            responses=self._ready_responses(),
            started=started,
            ignore_cancellation_until=release,
        )
        client = self._client(
            session, settings=_settings(meta_ads_mcp_timeout_seconds=0.05)
        )

        def probe() -> None:
            try:
                client.probe()
            except Exception as error:  # noqa: BLE001 - assert the safe public boundary.
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=0.5))
        bounded = finished.wait(timeout=0.3)
        release.set()
        thread.join(timeout=0.5)
        client.close()

        self.assertTrue(bounded)
        self.assertFalse(thread.is_alive())
        self.assertEqual([str(error) for error in errors], ["meta_timeout"])

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

    def test_response_budget_rejects_compressed_bodies_before_decompression(
        self,
    ) -> None:
        decoded = b"x" * 1_000
        for encoding, compressed in (
            ("gzip", gzip.compress(decoded)),
            ("deflate", zlib.compress(decoded)),
        ):
            with self.subTest(encoding=encoding):
                self.assertLess(len(compressed), 64)
                budget = _OperationBudget(64)
                transport = _ResponseBudgetTransport(
                    _StaticTransport(
                        [compressed],
                        headers={
                            "content-encoding": encoding,
                            "content-length": str(len(compressed)),
                        },
                    ),
                    lambda budget=budget: budget,
                )

                async def request(transport=transport) -> None:
                    async with httpx2.AsyncClient(transport=transport) as http_client:
                        response = await http_client.get("https://example.invalid")
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

    def test_forbidden_response_opens_the_local_authentication_circuit(self) -> None:
        request = httpx2.Request("POST", "https://mcp-facebook-ads.famachat.com.br/mcp")
        response = httpx2.Response(403, request=request)
        forbidden = _FakeSession(
            initialize_error=httpx2.HTTPStatusError(
                "forbidden", request=request, response=response
            )
        )
        client = self._client(forbidden)
        self.addCleanup(client.close)

        with self.assertRaisesRegex(MetaAdsError, "^meta_auth_unavailable$"):
            client.probe()
        with self.assertRaisesRegex(MetaAdsError, "^meta_auth_unavailable$"):
            client.probe()

        self.assertEqual(forbidden.initialize_calls, 1)

    def test_close_cancels_a_racing_request_and_concurrent_lifecycle_calls_are_safe(
        self,
    ) -> None:
        started = threading.Event()
        session = _FakeSession(
            responses=self._ready_responses(), pause_seconds=1.0, started=started
        )
        client = self._client(session)
        request_errors: list[BaseException] = []
        lifecycle_errors: list[BaseException] = []

        def request() -> None:
            try:
                client.probe()
            except Exception as error:  # noqa: BLE001 - asserts no exception crosses the boundary.
                request_errors.append(error)

        def lifecycle(operation: Callable[[], None]) -> None:
            try:
                operation()
            except Exception as error:  # noqa: BLE001 - asserts no exception crosses the boundary.
                lifecycle_errors.append(error)

        request_thread = threading.Thread(target=request)
        request_thread.start()
        self.assertTrue(started.wait(timeout=1.0))
        threads = [
            threading.Thread(target=lifecycle, args=(client.close,)),
            threading.Thread(target=lifecycle, args=(client.invalidate,)),
            threading.Thread(target=lifecycle, args=(client.close,)),
        ]
        for thread in threads:
            thread.start()
        request_thread.join(timeout=0.3)
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertFalse(request_thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(lifecycle_errors, [])
        self.assertEqual(len(request_errors), 1)
        self.assertIsInstance(request_errors[0], MetaAdsError)
        self.assertEqual(str(request_errors[0]), "meta_server_unavailable")

    def test_teardown_failures_are_contained_without_payload_leakage(self) -> None:
        broken = _FakeSession(
            responses=self._ready_responses(),
            teardown_error=RuntimeError("untrusted remote payload"),
        )
        replacement = _FakeSession(responses=self._ready_responses())
        client = self._client(broken, replacement)

        client.probe()
        client.invalidate()
        client.probe()
        client.close()

        self.assertEqual(broken.closed, 1)
        self.assertEqual(replacement.initialize_calls, 1)

    def test_hung_teardown_bounds_invalidate_and_close(self) -> None:
        settings = _settings(meta_ads_mcp_timeout_seconds=0.05)
        for operation_name in ("invalidate", "close"):
            with self.subTest(operation=operation_name):
                hanging = _FakeSession(
                    responses=self._ready_responses(), teardown_hangs=True
                )
                client = self._client(hanging, settings=settings)
                client.probe()

                started = time.monotonic()
                getattr(client, operation_name)()
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.3)
                with self.assertRaisesRegex(MetaAdsError, "^meta_server_unavailable$"):
                    client.probe()
                client.close()

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
