"""Read-only Streamable HTTP boundary for the remote Meta Ads MCP service.

The public API is synchronous because Brain's current application service is
synchronous.  MCP I/O remains confined to one private event-loop thread so a
single initialized session can be reused safely without exposing async state to
the rest of the service.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
import httpx2
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared._compat import resync_tracer
from mcp.shared._context_streams import create_context_streams
from mcp.shared.message import SessionMessage

from .config import BrainSettings
from .meta_ads_models import META_READ_TOOLS, MetaAdsError, RemoteAd, RemoteCampaign

_DECIMAL_ID = re.compile(r"^[0-9]{1,64}$", re.ASCII)
_MAX_CLEANUP_TIMEOUT_SECONDS = 0.25


class _SuppressStreamableHttpLogs(logging.Filter):
    """Prevent the SDK's debug serialization of MCP request/response values."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


_streamable_http_logger = logging.getLogger("mcp.client.streamable_http")
if not any(
    isinstance(log_filter, _SuppressStreamableHttpLogs)
    for log_filter in _streamable_http_logger.filters
):
    _streamable_http_logger.addFilter(_SuppressStreamableHttpLogs())


class _Session(Protocol):
    async def initialize(self) -> object: ...

    async def list_tools(self) -> object: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> object: ...


HttpClientFactory = Callable[..., Any]
SessionFactory = Callable[[Any], AbstractAsyncContextManager[_Session]]


@asynccontextmanager
async def _post_only_streamable_http_client(url: str, http_client: Any):
    """Use the SDK transport without its server-initiated GET/retry path.

    Brain makes only request/response MCP calls.  The SDK's convenience context
    manager starts a background GET stream after initialization and retries it;
    this composed transport intentionally supplies a no-op starter instead.
    It also deliberately omits the optional session-termination DELETE.
    """

    transport = StreamableHTTPTransport(url)
    read_stream_writer, read_stream = create_context_streams[
        SessionMessage | Exception
    ](0)
    write_stream, write_stream_reader = create_context_streams[SessionMessage](0)
    async with (
        read_stream_writer,
        read_stream,
        write_stream,
        write_stream_reader,
        anyio.create_task_group() as task_group,
    ):
        task_group.start_soon(
            transport.post_writer,
            http_client,
            write_stream_reader,
            read_stream_writer,
            write_stream,
            lambda: None,
            task_group,
        )
        try:
            yield read_stream, write_stream
        finally:
            task_group.cancel_scope.cancel()
    await resync_tracer()


@dataclass
class _OperationBudget:
    """Shared state for every HTTP response consumed by one MCP operation."""

    maximum_bytes: int
    seen_bytes: int = 0
    invalid_response: bool = False
    status_code: int | None = None

    def observe_response(self, response: httpx2.Response) -> None:
        self.status_code = response.status_code
        content_length = response.headers.get("content-length")
        if content_length is None:
            return
        try:
            length = int(content_length)
        except ValueError:
            self.invalid_response = True
            return
        if length < 0 or length > self.maximum_bytes - self.seen_bytes:
            self.invalid_response = True

    def consume(self, chunk: bytes) -> None:
        if len(chunk) > self.maximum_bytes - self.seen_bytes:
            self.invalid_response = True
            raise _ResponseTooLarge("remote response exceeded configured byte limit")
        self.seen_bytes += len(chunk)


class _ResponseTooLarge(httpx2.StreamError):
    """An internal stream error; the public layer maps it to a safe code."""


class _CountingAsyncByteStream(httpx2.AsyncByteStream):
    def __init__(
        self, stream: httpx2.AsyncByteStream, budget: _OperationBudget
    ) -> None:
        self._stream = stream
        self._budget = budget

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        if self._budget.invalid_response:
            raise _ResponseTooLarge("remote response exceeded configured byte limit")
        async for chunk in self._stream:
            self._budget.consume(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _ResponseBudgetTransport(httpx2.AsyncBaseTransport):
    """Apply the active operation's response limit before MCP parses a body."""

    def __init__(
        self,
        transport: httpx2.AsyncBaseTransport,
        active_budget: Callable[[], _OperationBudget | None],
    ) -> None:
        self._transport = transport
        self._active_budget = active_budget

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._transport.handle_async_request(request)
        budget = self._active_budget()
        if budget is None:
            return response
        budget.observe_response(response)
        if isinstance(response.stream, httpx2.AsyncByteStream):
            response.stream = _CountingAsyncByteStream(response.stream, budget)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


class RemoteMetaAdsMcpClient:
    """Synchronous, strict-allowlist facade around one remote MCP session."""

    def __init__(
        self,
        settings: BrainSettings,
        *,
        _http_client_factory: HttpClientFactory | None = None,
        _session_factory: SessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._http_client_factory = _http_client_factory or httpx2.AsyncClient
        self._session_factory = _session_factory or self._real_session_factory
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._loop_stopped = False
        self._operation_futures: set[concurrent.futures.Future[Any]] = set()
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="brain-meta-ads-mcp",
            daemon=True,
        )
        self._thread.start()
        self._loop_ready.wait()
        self._state_lock: asyncio.Lock | None = None
        self._session: _Session | None = None
        self._resources: AsyncExitStack | None = None
        self._ready = False
        self._auth_circuit_open = False
        self._active_budget: _OperationBudget | None = None

    def probe(self, deadline: float | None = None) -> None:
        self._submit("probe", None, deadline)

    def get_ad(self, source_id: str, deadline: float | None = None) -> RemoteAd:
        value = self._submit("ad", source_id, deadline)
        assert isinstance(value, RemoteAd)
        return value

    def get_campaign(
        self, campaign_id: str, deadline: float | None = None
    ) -> RemoteCampaign:
        value = self._submit("campaign", campaign_id, deadline)
        assert isinstance(value, RemoteCampaign)
        return value

    def invalidate(self) -> None:
        with self._lifecycle_lock:
            if self._closed or self._loop_stopped:
                return
            future = self._schedule_locked(self._invalidate_async())
        if future is None:
            return
        if not self._wait_for_cleanup(future):
            self._stop_loop_bounded()

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            for future in tuple(self._operation_futures):
                future.cancel()
            future = self._schedule_locked(self._close_async())
        if future is not None:
            self._wait_for_cleanup(future)
        self._stop_loop_bounded()

    def _stop_loop_bounded(self) -> None:
        with self._lifecycle_lock:
            if self._loop_stopped:
                return
            self._closed = True
            self._loop_stopped = True
            try:
                self._loop.call_soon_threadsafe(self._stop_after_pending_callbacks)
            except RuntimeError:
                return
        if threading.current_thread() is self._thread:
            return
        self._thread.join(timeout=self._cleanup_timeout_seconds())
        if not self._thread.is_alive() and not self._loop.is_closed():
            try:
                self._loop.close()
            except RuntimeError:
                return

    def _cleanup_timeout_seconds(self) -> float:
        return min(
            self._settings.meta_ads_mcp_timeout_seconds,
            _MAX_CLEANUP_TIMEOUT_SECONDS,
        )

    def _stop_after_pending_callbacks(self) -> None:
        self._loop.call_soon(self._loop.stop)

    def _wait_for_cleanup(self, future: concurrent.futures.Future[Any]) -> bool:
        try:
            future.result(timeout=self._cleanup_timeout_seconds())
        except concurrent.futures.TimeoutError:
            future.cancel()
            return False
        except (concurrent.futures.CancelledError, RuntimeError):
            return False
        except Exception:  # noqa: BLE001 - teardown errors are not public data.
            return True
        return True

    def _schedule_locked(self, coroutine: Any) -> concurrent.futures.Future[Any] | None:
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:
            coroutine.close()
            return

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def _run_coroutine(self, coroutine: Any) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _forget_operation(self, future: concurrent.futures.Future[Any]) -> None:
        with self._lifecycle_lock:
            self._operation_futures.discard(future)

    def _submit(
        self, operation: str, identifier: str | None, deadline: float | None
    ) -> RemoteAd | RemoteCampaign | None:
        if deadline is not None and deadline <= time.monotonic():
            raise MetaAdsError("meta_timeout")
        coroutine = self._operate(operation, identifier, deadline)
        with self._lifecycle_lock:
            if self._closed or self._loop_stopped:
                coroutine.close()
                raise MetaAdsError("meta_server_unavailable")
            future = self._schedule_locked(coroutine)
            if future is None:
                raise MetaAdsError("meta_server_unavailable")
            self._operation_futures.add(future)
            future.add_done_callback(self._forget_operation)
        try:
            timeout = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise MetaAdsError("meta_timeout") from error
        except (concurrent.futures.CancelledError, RuntimeError) as error:
            raise MetaAdsError("meta_server_unavailable") from error

    async def _operate(
        self, operation: str, identifier: str | None, deadline: float | None
    ) -> RemoteAd | RemoteCampaign | None:
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        async with self._state_lock:
            if self._auth_circuit_open:
                raise MetaAdsError("meta_auth_unavailable")
            self._active_budget = _OperationBudget(
                self._settings.meta_ads_mcp_response_max_bytes
            )
            try:
                if deadline is None:
                    return await self._operate_once(operation, identifier)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    return await self._operate_once(operation, identifier)
            except asyncio.CancelledError:
                await self._discard_session()
                raise
            except Exception as error:  # noqa: BLE001 - remote SDK failures are unbounded.
                mapped = self._map_error(error)
                if mapped.code == "meta_auth_unavailable":
                    self._auth_circuit_open = True
                if mapped.code in {
                    "meta_auth_unavailable",
                    "meta_invalid_response",
                    "meta_required_tool_unavailable",
                    "meta_server_unavailable",
                    "meta_timeout",
                    "meta_incomplete_result",
                    "meta_account_mismatch",
                }:
                    await self._discard_session()
                raise mapped from None
            finally:
                self._active_budget = None

    async def _operate_once(
        self, operation: str, identifier: str | None
    ) -> RemoteAd | RemoteCampaign | None:
        session = await self._ready_session()
        if operation == "probe":
            return None
        if operation == "ad":
            if not self._valid_identifier(identifier):
                raise MetaAdsError("meta_incomplete_result")
            return self._parse_ad(
                identifier,
                await self._call_structured(
                    session, "meta_get_ad", {"ad_id": identifier}
                ),
            )
        if operation == "campaign":
            if not self._valid_identifier(identifier):
                raise MetaAdsError("meta_incomplete_result")
            return self._parse_campaign(
                identifier,
                await self._call_structured(
                    session, "meta_get_campaign", {"campaign_id": identifier}
                ),
            )
        raise MetaAdsError("meta_invalid_response")

    async def _ready_session(self) -> _Session:
        session = await self._session_or_create()
        if self._ready:
            return session
        await session.initialize()
        listed = await session.list_tools()
        tools = getattr(listed, "tools", None)
        if not isinstance(tools, list) or not META_READ_TOOLS.issubset(
            {getattr(tool, "name", None) for tool in tools}
        ):
            raise MetaAdsError("meta_required_tool_unavailable")
        accounts = await self._call_structured(session, "meta_list_ad_accounts", {})
        self._verify_account(accounts)
        self._ready = True
        return session

    async def _call_structured(
        self, session: _Session, name: str, arguments: dict[str, Any]
    ) -> Mapping[str, Any]:
        if name not in META_READ_TOOLS:
            raise MetaAdsError("meta_required_tool_unavailable")
        result = await session.call_tool(name, arguments)
        if not isinstance(result, mcp_types.CallToolResult):
            raise MetaAdsError("meta_invalid_response")
        if result.is_error:
            raise MetaAdsError("meta_server_unavailable")
        if not isinstance(result.structured_content, Mapping):
            raise MetaAdsError("meta_invalid_response")
        return result.structured_content

    def _verify_account(self, result: Mapping[str, Any]) -> None:
        accounts = result.get("data")
        if not isinstance(accounts, list):
            raise MetaAdsError("meta_invalid_response")
        if len(accounts) != 1:
            raise MetaAdsError("meta_account_mismatch")
        account = accounts[0]
        if (
            not isinstance(account, Mapping)
            or account.get("id") != self._settings.meta_ad_account_id
        ):
            raise MetaAdsError("meta_account_mismatch")

    @staticmethod
    def _valid_identifier(value: object) -> bool:
        return isinstance(value, str) and _DECIMAL_ID.fullmatch(value) is not None

    @staticmethod
    def _parse_ad(source_id: str, result: Mapping[str, Any]) -> RemoteAd:
        if result.get("id") != source_id:
            raise MetaAdsError("meta_incomplete_result")
        try:
            return RemoteAd(
                result["id"],
                result["name"],
                result["campaign_id"],
                result["status"],
                result["effective_status"],
            )
        except (KeyError, TypeError, ValueError):
            raise MetaAdsError("meta_incomplete_result") from None

    @staticmethod
    def _parse_campaign(campaign_id: str, result: Mapping[str, Any]) -> RemoteCampaign:
        if result.get("id") != campaign_id:
            raise MetaAdsError("meta_incomplete_result")
        try:
            return RemoteCampaign(
                result["id"],
                result["name"],
                result["status"],
                result["effective_status"],
            )
        except (KeyError, TypeError, ValueError):
            raise MetaAdsError("meta_incomplete_result") from None

    async def _session_or_create(self) -> _Session:
        if self._session is not None:
            return self._session
        resources = AsyncExitStack()
        try:
            http_client = self._new_http_client()
            await resources.enter_async_context(http_client)
            session = await resources.enter_async_context(
                self._session_factory(http_client)
            )
        except BaseException:
            await resources.aclose()
            raise
        self._resources = resources
        self._session = session
        return session

    def _new_http_client(self) -> Any:
        transport = _ResponseBudgetTransport(
            httpx2.AsyncHTTPTransport(trust_env=False), lambda: self._active_budget
        )
        return self._http_client_factory(
            headers={"Authorization": f"Bearer {self._settings.meta_ads_mcp_api_key}"},
            timeout=self._settings.meta_ads_mcp_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            base_url=self._settings.meta_ads_mcp_url,
            transport=transport,
        )

    @asynccontextmanager
    async def _real_session_factory(self, http_client: Any):
        async with (
            _post_only_streamable_http_client(
                self._settings.meta_ads_mcp_url,
                http_client=http_client,
            ) as streams,
            ClientSession(*streams) as session,
        ):
            yield session

    async def _invalidate_async(self) -> None:
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        async with self._state_lock:
            await self._discard_session()
            self._auth_circuit_open = False

    async def _close_async(self) -> None:
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        async with self._state_lock:
            await self._discard_session()

    async def _discard_session(self) -> None:
        resources, self._resources = self._resources, None
        self._session = None
        self._ready = False
        if resources is not None:
            try:
                await resources.aclose()
            except Exception:  # noqa: BLE001 - teardown errors carry untrusted data.
                return

    def _map_error(self, error: BaseException) -> MetaAdsError:
        budget = self._active_budget
        if budget is not None:
            if budget.invalid_response:
                return MetaAdsError("meta_invalid_response")
            if budget.status_code in {401, 403}:
                return MetaAdsError("meta_auth_unavailable")
            if budget.status_code == 429:
                return MetaAdsError("meta_rate_limited")
            if budget.status_code == 404:
                return MetaAdsError("meta_not_found")
        if isinstance(error, MetaAdsError):
            return error
        if isinstance(error, (asyncio.TimeoutError, httpx2.TimeoutException)):
            return MetaAdsError("meta_timeout")
        if isinstance(error, httpx2.HTTPStatusError):
            status = error.response.status_code
            if status in {401, 403}:
                return MetaAdsError("meta_auth_unavailable")
            if status == 429:
                return MetaAdsError("meta_rate_limited")
            if status == 404:
                return MetaAdsError("meta_not_found")
        return MetaAdsError("meta_server_unavailable")
