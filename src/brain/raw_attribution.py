"""Validation and safe comparison for captured raw CTWA attribution."""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .transport_models import RuntimeIds

_BASE64_RE = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
)
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_MAX_SAFE_LENGTH = 10_000_000
_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True)
class RawAttributionLimits:
    max_bytes: int = 4 * 1024 * 1024
    max_depth: int = 32
    max_nodes: int = 10_000

    def __post_init__(self) -> None:
        for value in (self.max_bytes, self.max_depth, self.max_nodes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RawAttributionError("raw_type")


class RawAttributionError(ValueError):
    """A raw capture was invalid; ``code`` is safe to surface operationally."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class _State:
    limits: RawAttributionLimits
    nodes: int = 0


def _string(value: str) -> str:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise RawAttributionError("raw_unicode")
    return value


def _codepoint_length(value: str) -> int:
    """Python strings already iterate Unicode code points, unlike UTF-16 JS."""
    return len(value)


def _node(state: _State, depth: int) -> None:
    if depth > state.limits.max_depth:
        raise RawAttributionError("raw_depth")
    state.nodes += 1
    if state.nodes > state.limits.max_nodes:
        raise RawAttributionError("raw_nodes")


def _tagged(value: dict[str, object]) -> dict[str, str] | None:
    if "$type" not in value:
        return None
    if set(value) != {"$type", "encoding", "data"}:
        raise RawAttributionError("raw_tag")
    tag, encoding, data = value["$type"], value["encoding"], value["data"]
    if tag == "bytes" and encoding == "base64" and isinstance(data, str):
        _string(data)
        if not _BASE64_RE.fullmatch(data):
            raise RawAttributionError("raw_tag")
        try:
            if (
                base64.b64encode(base64.b64decode(data, validate=True)).decode("ascii")
                != data
            ):
                raise RawAttributionError("raw_tag")
        except ValueError:
            raise RawAttributionError("raw_tag") from None
        return {"$type": "bytes", "encoding": "base64", "data": data}
    if tag == "integer" and encoding == "decimal" and isinstance(data, str):
        _string(data)
        if _DECIMAL_RE.fullmatch(data):
            return {"$type": "integer", "encoding": "decimal", "data": data}
    raise RawAttributionError("raw_tag")


def _validate_value(value: object, depth: int, state: _State) -> object:
    _node(state, depth)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (int, float)):
        if (
            isinstance(value, bool)
            or not math.isfinite(value)
            or (isinstance(value, int) and abs(value) > _MAX_SAFE_INTEGER)
            or (isinstance(value, float) and value == 0 and math.copysign(1, value) < 0)
        ):
            raise RawAttributionError("raw_type")
        return value
    if isinstance(value, list):
        return [_validate_value(child, depth + 1, state) for child in value]
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise RawAttributionError("raw_type")
            _string(key)
        tagged = _tagged(value)
        if tagged is not None:
            return tagged
        return {
            key: _validate_value(value[key], depth + 1, state) for key in sorted(value)
        }
    raise RawAttributionError("raw_type")


def canonicalize_raw_attribution(value: object, limits: RawAttributionLimits) -> str:
    """Validate a decoded raw tree and return its deterministic JSON encoding."""
    if not isinstance(limits, RawAttributionLimits):
        raise RawAttributionError("raw_type")
    validated = _validate_value(value, 0, _State(limits))
    try:
        encoded = json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise RawAttributionError("raw_type") from None
    if len(encoded.encode("utf-8")) > limits.max_bytes:
        raise RawAttributionError("raw_size")
    return encoded


def _reject_constant(_: str) -> object:
    raise RawAttributionError("raw_type")


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RawAttributionError("raw_canonical")
        result[key] = value
    return result


def decode_canonical_raw_attribution(
    encoded: object, limits: RawAttributionLimits | None = None
) -> object:
    """Decode only a byte-for-byte canonical raw-attribution JSON document."""
    if not isinstance(encoded, str):
        raise RawAttributionError("raw_type")
    checked_limits = RawAttributionLimits() if limits is None else limits
    try:
        decoded = json.loads(
            encoded,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_object,
        )
    except (json.JSONDecodeError, RecursionError):
        raise RawAttributionError("raw_json") from None
    canonical = canonicalize_raw_attribution(decoded, checked_limits)
    if encoded != canonical:
        raise RawAttributionError("raw_canonical")
    return decoded


def _normalized_value(normalized: Mapping[str, object], name: str) -> object:
    return normalized.get(name)


def _metadata_text(value: object, maximum: int = 128) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or _codepoint_length(value) > maximum
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return None
    return value


def _opaque(value: object, ids: RuntimeIds) -> tuple[bool, int | None, str | None]:
    if (
        not isinstance(value, str)
        or not value
        or _codepoint_length(value) > _MAX_SAFE_LENGTH
    ):
        return False, None, None
    return True, _codepoint_length(value), ids.opaque_hmac(value)


def _source_url(
    value: object, ids: RuntimeIds
) -> tuple[str | None, int | None, str | None]:
    if (
        not isinstance(value, str)
        or not value
        or _codepoint_length(value) > _MAX_SAFE_LENGTH
    ):
        return None, None, None
    try:
        hostname = urlsplit(value).hostname
    except (TypeError, ValueError):
        return None, None, None
    if hostname is None:
        return None, None, None
    hostname = hostname.lower()
    if _codepoint_length(hostname) > 253 or not _HOSTNAME_RE.fullmatch(hostname):
        return None, None, None
    return hostname, _codepoint_length(value), ids.opaque_hmac(value)


def _require(normalized: Mapping[str, object], expected: Mapping[str, object]) -> None:
    if any(
        _normalized_value(normalized, name) != value for name, value in expected.items()
    ):
        raise RawAttributionError("raw_normalized_mismatch")


def assert_raw_matches_normalized(
    raw: object, normalized: object, ids: RuntimeIds
) -> None:
    """Require raw known fields to reproduce observer normalized evidence."""
    if not isinstance(raw, Mapping) or not isinstance(normalized, Mapping):
        raise RawAttributionError("raw_normalized_mismatch")

    _require(
        normalized,
        {
            "source_type": _metadata_text(raw.get("sourceType")),
            "source_app": _metadata_text(raw.get("sourceApp")),
        },
    )
    source_present, source_length, source_hmac = _opaque(raw.get("sourceId"), ids)
    _require(
        normalized,
        {
            "source_id_present": source_present,
            "source_id_length": source_length,
            "source_id_hmac": source_hmac,
        },
    )
    clid_present, clid_length, clid_hmac = _opaque(raw.get("ctwaClid"), ids)
    _require(
        normalized,
        {
            "ctwa_clid_present": clid_present,
            "ctwa_clid_length": clid_length,
            "ctwa_clid_hmac": clid_hmac,
        },
    )
    hostname, url_length, url_hmac = _source_url(raw.get("sourceUrl"), ids)
    _require(
        normalized,
        {
            "source_url_hostname": hostname,
            "source_url_length": url_length,
            "source_url_hmac": url_hmac,
        },
    )
    for raw_name, normalized_name in (
        ("showAdAttribution", "show_ad_attribution"),
        ("clickToWhatsappCall", "click_to_whatsapp_call"),
        ("containsAutoReply", "contains_auto_reply"),
    ):
        value = raw.get(raw_name)
        _require(
            normalized, {normalized_name: value if isinstance(value, bool) else None}
        )
