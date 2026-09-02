"""Strict, read-only boundary for Meta's hosted Ads MCP server.

This module intentionally has no fallback to the Graph API and never derives
tools, accounts, filters, or fields from external text.  The client accepts
only structured MCP responses and converts all external failures into the
bounded error vocabulary defined by :mod:`brain.meta_ads_models`.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

import anyio
import httpx2
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

from .config import BrainSettings
from .meta_ads_models import (
    META_ADS_MCP_URL,
    MetaAdRecord,
    MetaAdsCapabilities,
    MetaAdsError,
    canonical_account_id,
)

READ_TOOLS = frozenset(
    {
        "ads_get_ad_accounts",
        "ads_get_field_context",
        "ads_get_ad_entities",
        "ads_get_creatives",
    }
)
_REQUIRED_TOOLS = frozenset({"ads_get_ad_accounts", "ads_get_ad_entities"})
_MAX_PAGES = 100
_MAX_ENTITIES_PER_PAGE = 500
_MAX_COLLECTION_ITEMS = 500
_MAX_RETRY_AFTER_SECONDS = 86_400.0
_FIXED_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_DECIMAL_ID = re.compile(r"^[0-9]{1,64}$", re.ASCII)
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]+$", re.DOTALL)
_AD_FIELDS = (
    "id",
    "account_id",
    "name",
    "status",
    "effective_status",
    "adset{id,name,status}",
    "campaign{id,name,status}",
    "creative{id,name}",
)

_Session: TypeAlias = Any
SessionFactory: TypeAlias = Callable[[], AbstractAsyncContextManager[_Session]]


class MetaAdsClient(Protocol):
    """The deterministic remote boundary used by attribution orchestration."""

    def probe(self) -> MetaAdsCapabilities: ...

    def get_ad(
        self,
        source_id: str,
        now: float,
        *,
        timeout_seconds: float | None = None,
    ) -> MetaAdRecord: ...

    def list_ads(self, now: float, full: bool) -> list[MetaAdRecord]: ...


class OAuthCredentialProvider(Protocol):
    """Runtime-only bearer-token source for the explicit OAuth mode."""

    def access_token(self, now: float) -> str: ...

    async def access_token_async(self, now: float, budget_seconds: float) -> str: ...

    def invalidate(self) -> None: ...


class _ResponseByteBudget:
    """Mutable response budget shared by every HTTP response in one operation."""

    def __init__(self, configured_maximum_bytes: int) -> None:
        if (
            isinstance(configured_maximum_bytes, bool)
            or not isinstance(configured_maximum_bytes, int)
            or configured_maximum_bytes <= 0
        ):
            raise ValueError("response byte budget must be positive")
        self._maximum_bytes = min(configured_maximum_bytes, _FIXED_RESPONSE_MAX_BYTES)
        self._seen = 0

    def consume(self, size: int) -> None:
        self._seen += size
        if self._seen > self._maximum_bytes:
            raise MetaAdsError("meta_invalid_response")


class _CountingStream(httpx2.AsyncByteStream):
    def __init__(
        self, stream: httpx2.AsyncByteStream, response_budget: _ResponseByteBudget
    ) -> None:
        self._stream = stream
        self._response_budget = response_budget

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._response_budget.consume(len(chunk))
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _ResponseLimitTransport(httpx2.AsyncBaseTransport):
    """Wrap every MCP HTTP response stream with a per-operation byte limit."""

    def __init__(
        self,
        transport: httpx2.AsyncBaseTransport,
        response_budget: _ResponseByteBudget | int,
    ) -> None:
        self._transport = transport
        self._response_budget = (
            _ResponseByteBudget(response_budget)
            if isinstance(response_budget, int)
            else response_budget
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._transport.handle_async_request(request)
        return httpx2.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CountingStream(response.stream, self._response_budget),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


def _object_properties(schema: object) -> dict[str, object] | None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    return properties


def _declares_data_array(schema: object) -> bool:
    properties = _object_properties(schema)
    if properties is None:
        return False
    data = properties.get("data")
    return isinstance(data, dict) and data.get("type") == "array"


def _safe_text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or _SAFE_TEXT.fullmatch(value) is None
    ):
        raise MetaAdsError("meta_incomplete_result")
    return value


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MetaAdsError("meta_invalid_response")
    return cast(Mapping[str, object], value)


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DECIMAL_ID.fullmatch(value) is None:
        raise MetaAdsError("meta_invalid_response")
    return value


def _required_id(value: object) -> str:
    identifier = _optional_id(value)
    if identifier is None:
        raise MetaAdsError("meta_incomplete_result")
    return identifier


def _next_cursor(payload: Mapping[str, object]) -> str | None:
    paging = payload.get("paging")
    if paging is None:
        return None
    if not isinstance(paging, dict):
        raise MetaAdsError("meta_invalid_response")
    cursors = paging.get("cursors")
    if not isinstance(cursors, dict):
        raise MetaAdsError("meta_invalid_response")
    after = cursors.get("after")
    if after is None:
        return None
    if not isinstance(after, str) or not after or len(after.encode("utf-8")) > 512:
        raise MetaAdsError("meta_invalid_response")
    return after


@dataclass(frozen=True)
class _ToolAdapter:
    account_argument: str
    entity_selector_argument: str
    exact_id_argument_declared: bool
    field_context_tool: mcp_types.Tool | None


class MetaAdsMcpClient:
    """MCP SDK client pinned to one Meta Ads account and read operations."""

    def __init__(
        self,
        settings: BrainSettings,
        *,
        session_factory: SessionFactory | None = None,
        credential_provider: OAuthCredentialProvider | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = canonical_account_id(settings.meta_ad_account_id)
        self._session_factory = session_factory
        self._credential_provider = credential_provider
        self._capabilities: MetaAdsCapabilities | None = None

    def probe(self) -> MetaAdsCapabilities:
        return self._run_with_oauth_retry(self._probe)

    def get_ad(
        self,
        source_id: str,
        now: float,
        *,
        timeout_seconds: float | None = None,
    ) -> MetaAdRecord:
        self._validate_source_id(source_id)
        self._validate_now(now)
        timeout_seconds = self._context_timeout(timeout_seconds)
        return self._run_with_oauth_retry(self._get_ad, source_id, now, timeout_seconds)

    def list_ads(self, now: float, full: bool) -> list[MetaAdRecord]:
        self._validate_now(now)
        if not isinstance(full, bool):
            raise TypeError("full must be boolean")
        return self._run_with_oauth_retry(self._list_ads, now, full)

    def _run_with_oauth_retry(self, function: Callable[..., Any], *args: object) -> Any:
        try:
            return self._run_sync(function, *args)
        except MetaAdsError as exc:
            if exc.code != "meta_auth_unavailable" or self._credential_provider is None:
                raise
            self._credential_provider.invalidate()
            return self._run_sync(function, *args)

    @staticmethod
    def _validate_source_id(source_id: object) -> None:
        if not isinstance(source_id, str) or _DECIMAL_ID.fullmatch(source_id) is None:
            raise MetaAdsError("meta_invalid_response")

    @staticmethod
    def _validate_now(now: object) -> None:
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(now)
        ):
            raise ValueError("now must be finite")

    def _context_timeout(self, timeout_seconds: float | None) -> float:
        if timeout_seconds is None:
            return self._settings.meta_ads_mcp_timeout_seconds
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise MetaAdsError("meta_invalid_response")
        return min(float(timeout_seconds), self._settings.meta_ads_mcp_timeout_seconds)

    @staticmethod
    def _run_sync(function: Callable[..., Any], *args: object) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return anyio.run(function, *args)
        raise RuntimeError("Meta Ads MCP client must run outside an event loop")

    def _authorization_token(self, now: float) -> str:
        if self._credential_provider is not None:
            try:
                return self._credential_provider.access_token(now)
            except Exception:  # noqa: BLE001 - secret boundary exposes no details
                raise MetaAdsError("meta_auth_unavailable") from None
        if self._settings.meta_ads_mcp_auth_mode == "oauth":
            raise MetaAdsError("meta_auth_unavailable")
        token = self._settings.meta_ads_mcp_access_token
        if not token:
            raise MetaAdsError("meta_auth_unavailable")
        return token

    async def _authorization_token_async(
        self, now: float, timeout_seconds: float
    ) -> str:
        if self._credential_provider is None:
            return self._authorization_token(now)
        deadline = anyio.current_effective_deadline()
        remaining = timeout_seconds
        if math.isfinite(deadline):
            remaining = min(remaining, deadline - anyio.current_time())
        if remaining <= 0:
            raise MetaAdsError("meta_timeout")
        try:
            with anyio.fail_after(remaining):
                return await self._credential_provider.access_token_async(
                    now, remaining
                )
        except TimeoutError:
            raise MetaAdsError("meta_timeout") from None
        except MetaAdsError:
            raise
        except Exception:  # noqa: BLE001 - secret boundary exposes no details
            raise MetaAdsError("meta_auth_unavailable") from None

    @asynccontextmanager
    async def _session(
        self, timeout_seconds: float | None = None
    ) -> AsyncIterator[_Session]:
        if self._session_factory is not None:
            try:
                async with self._session_factory() as session:
                    yield session
            except MetaAdsError:
                raise
            except Exception as exc:  # noqa: BLE001 - remote SDK boundary
                raise self._map_error(exc) from None
            return

        timeout_seconds = self._context_timeout(timeout_seconds)
        token = await self._authorization_token_async(time.time(), timeout_seconds)
        transport = _ResponseLimitTransport(
            httpx2.AsyncHTTPTransport(),
            _ResponseByteBudget(self._settings.meta_ads_mcp_response_max_bytes),
        )
        try:
            async with (
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx2.Timeout(timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                    transport=transport,
                ) as http,
                streamable_http_client(
                    META_ADS_MCP_URL,
                    http_client=http,
                    terminate_on_close=True,
                ) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timeout_seconds,
                ) as session,
            ):
                yield session
        except MetaAdsError:
            raise
        except Exception as exc:  # noqa: BLE001 - remote SDK boundary
            raise self._map_error(exc) from None

    async def _probe(self) -> MetaAdsCapabilities:
        async with self._session(
            self._settings.meta_ads_mcp_timeout_seconds
        ) as session:
            return await self._probe_session(session)

    async def _probe_session(self, session: _Session) -> MetaAdsCapabilities:
        try:
            await session.initialize()
            listed = await session.list_tools()
            tools = getattr(listed, "tools", None)
            adapter = self._validate_tools(tools)
            accounts_result = await self._call_tool(session, "ads_get_ad_accounts", {})
            accounts = self._structured_data(accounts_result)
            self._validate_account_presence(accounts)
            exact_id_filter_supported = await self._direct_id_filter_supported(
                session, adapter
            )
        except MetaAdsError:
            raise
        except Exception as exc:  # noqa: BLE001 - remote SDK boundary
            raise self._map_error(exc) from None
        capabilities = MetaAdsCapabilities(
            account_id=self._account_id,
            required_tools=_REQUIRED_TOOLS,
            account_argument=adapter.account_argument,
            entity_selector_argument=adapter.entity_selector_argument,
            result_array_path=("data",),
            exact_id_filter_supported=exact_id_filter_supported,
        )
        self._capabilities = capabilities
        return capabilities

    def _validate_tools(self, tools: object) -> _ToolAdapter:
        if not isinstance(tools, list):
            raise MetaAdsError("meta_required_tool_unavailable")
        by_name: dict[str, mcp_types.Tool] = {}
        for tool in tools:
            if not isinstance(tool, mcp_types.Tool) or tool.name in by_name:
                raise MetaAdsError("meta_required_tool_unavailable")
            by_name[tool.name] = tool
        if not _REQUIRED_TOOLS.issubset(by_name):
            raise MetaAdsError("meta_required_tool_unavailable")
        accounts = by_name["ads_get_ad_accounts"]
        entities = by_name["ads_get_ad_entities"]
        if not _declares_data_array(accounts.output_schema) or not _declares_data_array(
            entities.output_schema
        ):
            raise MetaAdsError("meta_required_tool_unavailable")
        properties = _object_properties(entities.input_schema)
        if properties is None:
            raise MetaAdsError("meta_required_tool_unavailable")
        account_argument = next(
            (
                name
                for name in ("ad_account_id", "account_id")
                if isinstance(properties.get(name), dict)
                and properties[name].get("type") == "string"
            ),
            None,
        )
        selector_argument = next(
            (
                name
                for name in ("entity_type", "level")
                if isinstance(properties.get(name), dict)
                and properties[name].get("type") == "string"
            ),
            None,
        )
        if account_argument is None or selector_argument is None:
            raise MetaAdsError("meta_required_tool_unavailable")
        for name, expected_type in (
            ("fields", "array"),
            ("limit", "integer"),
            ("after", "string"),
        ):
            if (
                not isinstance(properties.get(name), dict)
                or properties[name].get("type") != expected_type
            ):
                raise MetaAdsError("meta_required_tool_unavailable")
        return _ToolAdapter(
            account_argument=account_argument,
            entity_selector_argument=selector_argument,
            exact_id_argument_declared=isinstance(properties.get("id"), dict)
            and properties["id"].get("type") == "string",
            field_context_tool=by_name.get("ads_get_field_context"),
        )

    async def _direct_id_filter_supported(
        self, session: _Session, adapter: _ToolAdapter
    ) -> bool:
        """Accept direct filtering only after a structured field-context proof."""
        tool = adapter.field_context_tool
        if tool is None or not adapter.exact_id_argument_declared:
            return False
        arguments = self._field_context_arguments(tool)
        if arguments is None or not _declares_data_array(tool.output_schema):
            return False
        try:
            result = await self._call_tool(session, "ads_get_field_context", arguments)
            fields = self._structured_data(result)
        except MetaAdsError:
            return False
        for field in fields:
            if (
                field.get("entity_type") == "ad"
                and field.get("field") == "id"
                and field.get("filterable") is True
            ):
                return True
        return False

    def _field_context_arguments(
        self, tool: mcp_types.Tool
    ) -> dict[str, object] | None:
        properties = _object_properties(tool.input_schema)
        if properties is None:
            return None
        selector_argument = next(
            (
                name
                for name in ("entity_type", "level")
                if isinstance(properties.get(name), dict)
                and properties[name].get("type") == "string"
            ),
            None,
        )
        if (
            selector_argument is None
            or not isinstance(properties.get("field"), dict)
            or properties["field"].get("type") != "string"
        ):
            return None
        arguments: dict[str, object] = {selector_argument: "ad", "field": "id"}
        account_argument = next(
            (
                name
                for name in ("ad_account_id", "account_id")
                if isinstance(properties.get(name), dict)
                and properties[name].get("type") == "string"
            ),
            None,
        )
        if account_argument is not None:
            arguments[account_argument] = f"act_{self._account_id}"
        required = tool.input_schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(name, str) for name in required
        ):
            return None
        if any(name not in arguments for name in required):
            return None
        return arguments

    @staticmethod
    async def _call_tool(
        session: _Session, tool_name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        if tool_name not in READ_TOOLS:
            raise MetaAdsError("meta_required_tool_unavailable")
        try:
            result = await session.call_tool(tool_name, arguments)
        except MetaAdsError:
            raise
        except Exception as exc:  # noqa: BLE001 - remote SDK boundary
            raise MetaAdsMcpClient._map_error(exc) from None
        if not isinstance(result, mcp_types.CallToolResult) or result.is_error:
            raise MetaAdsError("meta_server_unavailable")
        return result

    @staticmethod
    def _structured_data(
        result: mcp_types.CallToolResult,
    ) -> list[Mapping[str, object]]:
        structured = result.structured_content
        if not isinstance(structured, dict):
            raise MetaAdsError("meta_invalid_response")
        data = structured.get("data")
        if not isinstance(data, list) or len(data) > _MAX_COLLECTION_ITEMS:
            raise MetaAdsError("meta_invalid_response")
        items: list[Mapping[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                raise MetaAdsError("meta_invalid_response")
            items.append(cast(Mapping[str, object], item))
        return items

    def _validate_account_presence(
        self, accounts: Sequence[Mapping[str, object]]
    ) -> None:
        matches = 0
        for account in accounts:
            candidate = account.get("id", account.get("account_id"))
            if not isinstance(candidate, str):
                raise MetaAdsError("meta_invalid_response")
            try:
                normalized = canonical_account_id(candidate)
            except ValueError:
                continue
            if normalized == self._account_id:
                matches += 1
        if matches != 1:
            raise MetaAdsError("meta_account_mismatch")

    async def _get_ad(
        self, source_id: str, now: float, timeout_seconds: float
    ) -> MetaAdRecord:
        try:
            with anyio.fail_after(timeout_seconds):
                async with self._session(timeout_seconds) as session:
                    capabilities = await self._probe_session(session)
                    records = await self._read_entity_pages(
                        session,
                        capabilities,
                        now,
                        source_id=source_id,
                        full=False,
                    )
        except TimeoutError:
            raise MetaAdsError("meta_timeout") from None
        matching = [record for record in records if record.ad_id == source_id]
        if not matching:
            raise MetaAdsError("meta_not_found")
        if len(matching) != 1:
            raise MetaAdsError("meta_invalid_response")
        return matching[0]

    async def _list_ads(self, now: float, full: bool) -> list[MetaAdRecord]:
        async with self._session(
            self._settings.meta_ads_mcp_timeout_seconds
        ) as session:
            capabilities = await self._probe_session(session)
            return await self._read_entity_pages(
                session, capabilities, now, source_id=None, full=full
            )

    async def _read_entity_pages(
        self,
        session: _Session,
        capabilities: MetaAdsCapabilities,
        now: float,
        *,
        source_id: str | None,
        full: bool,
    ) -> list[MetaAdRecord]:
        maximum_pages = _MAX_PAGES if source_id is not None or full else 1
        cursor: str | None = None
        seen_cursors: set[str] = set()
        records: list[MetaAdRecord] = []
        for _ in range(maximum_pages):
            arguments: dict[str, object] = {
                capabilities.account_argument: f"act_{self._account_id}",
                capabilities.entity_selector_argument: "ad",
                "fields": list(_AD_FIELDS),
                "limit": _MAX_ENTITIES_PER_PAGE,
            }
            if cursor is not None:
                arguments["after"] = cursor
            if source_id is not None and capabilities.exact_id_filter_supported:
                arguments["id"] = source_id
            result = await self._call_tool(session, "ads_get_ad_entities", arguments)
            items = self._structured_data(result)
            for item in items:
                item_id = item.get("id")
                if (
                    not isinstance(item_id, str)
                    or _DECIMAL_ID.fullmatch(item_id) is None
                ):
                    raise MetaAdsError("meta_invalid_response")
                if source_id is not None and item_id != source_id:
                    continue
                record = self._normalize_record(item, now)
                records.append(record)
                if source_id is not None and len(records) > 1:
                    raise MetaAdsError("meta_invalid_response")
            payload = result.structured_content
            if not isinstance(payload, dict):
                raise MetaAdsError("meta_invalid_response")
            next_cursor = _next_cursor(payload)
            if source_id is not None and records:
                return records
            if next_cursor is None:
                return records
            if next_cursor in seen_cursors:
                raise MetaAdsError("meta_invalid_response")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MetaAdsError("meta_invalid_response")

    def _normalize_record(
        self, value: Mapping[str, object], now: float
    ) -> MetaAdRecord:
        try:
            account_id = canonical_account_id(value.get("account_id"))
        except ValueError:
            raise MetaAdsError("meta_account_mismatch") from None
        if account_id != self._account_id:
            raise MetaAdsError("meta_account_mismatch")
        adset = _optional_mapping(value.get("adset"))
        creative = _optional_mapping(value.get("creative"))
        campaign = _optional_mapping(value.get("campaign"))
        if campaign is None:
            raise MetaAdsError("meta_incomplete_result")
        adset_id = None if adset is None else _required_id(adset.get("id"))
        adset_name = None if adset is None else _safe_text(adset.get("name"))
        creative_id = None if creative is None else _required_id(creative.get("id"))
        creative_name = None if creative is None else _safe_text(creative.get("name"))
        try:
            return MetaAdRecord(
                account_id=account_id,
                ad_id=_required_id(value.get("id")),
                ad_name=cast(str, _safe_text(value.get("name"))),
                ad_status=_safe_text(value.get("status"), nullable=True),
                ad_effective_status=_safe_text(
                    value.get("effective_status"), nullable=True
                ),
                adset_id=adset_id,
                adset_name=cast(str | None, adset_name),
                adset_status=None
                if adset is None
                else _safe_text(adset.get("status"), nullable=True),
                campaign_id=_required_id(campaign.get("id")),
                campaign_name=cast(str, _safe_text(campaign.get("name"))),
                campaign_status=_safe_text(campaign.get("status"), nullable=True),
                creative_id=creative_id,
                creative_name=cast(str | None, creative_name),
                metadata_complete=adset is not None and creative is not None,
                fetched_at=now,
            )
        except MetaAdsError:
            raise
        except (TypeError, ValueError):
            raise MetaAdsError("meta_invalid_response") from None

    @staticmethod
    def _map_error(error: Exception) -> MetaAdsError:
        if isinstance(error, MetaAdsError):
            return error
        if isinstance(error, (httpx2.TimeoutException, TimeoutError)):
            return MetaAdsError("meta_timeout")
        if isinstance(error, httpx2.HTTPStatusError):
            status = error.response.status_code
            if status in {401, 403}:
                return MetaAdsError("meta_auth_unavailable")
            if status == 429:
                return MetaAdsError(
                    "meta_rate_limited",
                    MetaAdsMcpClient._retry_after(
                        error.response.headers.get("Retry-After")
                    ),
                )
            if 500 <= status <= 599:
                return MetaAdsError("meta_server_unavailable")
            return MetaAdsError("meta_invalid_response")
        return MetaAdsError("meta_server_unavailable")

    @staticmethod
    def _retry_after(value: object) -> float | None:
        if not isinstance(value, str):
            return None
        try:
            delay = float(value)
        except ValueError:
            return None
        if not math.isfinite(delay) or not 0 <= delay <= _MAX_RETRY_AFTER_SECONDS:
            return None
        return delay
