"""Clean external transcript projection, compaction handling and dedupe."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProjectedMessage:
    message_id: int
    role: str
    timestamp: float
    text: str
    active: int

    @property
    def speaker(self) -> str:
        return "cliente" if self.role == "user" else "fama"

    @property
    def trust(self) -> str:
        return (
            "untrusted_external_data"
            if self.role == "user"
            else "prior_conversation_data"
        )


CONTENT_JSON_PREFIX = "\x00json:"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return _text(value.decode("utf-8", errors="replace"))
    if isinstance(value, str):
        if value.startswith(CONTENT_JSON_PREFIX):
            try:
                return _text(json.loads(value[len(CONTENT_JSON_PREFIX) :]))
            except (json.JSONDecodeError, TypeError):
                # Match Hermes' fail-safe behavior for malformed legacy rows.
                return value
        return value
    if isinstance(value, (list, tuple)):
        parts = [part for item in value if (part := _text(item))]
        return "\n".join(parts) or None
    if isinstance(value, dict):
        direct_text = value.get("text")
        if isinstance(direct_text, str):
            return direct_text
        if "content" in value:
            return _text(value["content"])
        # Image URLs and provider metadata are not conversation text.
        return None
    return str(value)


def _dedupe_content(value: Any) -> Any:
    """Return a hashable representation of the stored content itself."""
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return value


def project_rows(rows: Iterable[Mapping[str, Any]]) -> list[ProjectedMessage]:
    """Apply the six-step V1 projection and dedupe before pagination."""
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        active = int(row["active"] or 0)
        compacted = int(row["compacted"] or 0)
        if not (active == 1 or compacted == 1):
            continue
        if int(row["_compressed_summary"] or 0) == 1:
            continue
        if row["display_kind"] is not None:
            continue
        role = str(row["role"])
        if role not in {"user", "assistant"}:
            continue
        content = _text(row["content"])
        if not content or not content.strip():
            continue
        if row["tool_name"] is not None:
            continue
        # Assistant rows that contain only tool calls have no external text
        # and were excluded above. If a provider stores real text alongside a
        # tool call, preserve that text as required by the projection contract.
        key = (role, _dedupe_content(row["content"]), row["timestamp"])
        current = deduped.get(key)
        candidate = dict(row)
        if current is None or (active, int(row["id"])) > (
            int(current["active"] or 0),
            int(current["id"]),
        ):
            deduped[key] = candidate

    return [
        ProjectedMessage(
            message_id=int(row["id"]),
            role=str(row["role"]),
            timestamp=float(row["timestamp"]),
            text=str(_text(row["content"])),
            active=int(row["active"] or 0),
        )
        for row in sorted(deduped.values(), key=lambda item: int(item["id"]))
    ]
