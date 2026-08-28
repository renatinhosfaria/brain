"""Fail-closed WhatsApp JID to transport-phone resolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_PHONE_JID_RE = re.compile(r"^(?P<phone>[1-9][0-9]{6,14})@s\.whatsapp\.net$")
_LID_JID_RE = re.compile(r"^(?P<lid>[0-9]{1,20})@lid$")
_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
_LID_RE = re.compile(r"^[0-9]{1,20}$")
_FORWARD_FILE_RE = re.compile(
    r"^lid-mapping-(?P<phone>[1-9][0-9]{6,14})\.json$"
)
_REVERSE_FILE_RE = re.compile(
    r"^lid-mapping-(?P<lid>[0-9]{1,20})_reverse\.json$"
)
_MAPPING_FILE_PREFIX = "lid-mapping-"


@dataclass(frozen=True)
class PhoneResolution:
    status: Literal["ok", "unavailable"]
    phone: str | None
    reason: str


def _unavailable(reason: str) -> PhoneResolution:
    return PhoneResolution(status="unavailable", phone=None, reason=reason)


def _parse_mapping_file(path: Path, filename: str) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("mapping file is unreadable or invalid JSON") from exc
    if not isinstance(payload, str):
        raise TypeError("mapping value must be a JSON string")

    forward = _FORWARD_FILE_RE.fullmatch(filename)
    if forward:
        phone = forward.group("phone")
        if not _LID_RE.fullmatch(payload):
            raise ValueError("forward mapping does not contain a numeric LID")
        return payload, phone

    reverse = _REVERSE_FILE_RE.fullmatch(filename)
    if reverse:
        lid = reverse.group("lid")
        if not _PHONE_RE.fullmatch(payload):
            raise ValueError("reverse mapping does not contain a numeric phone")
        return lid, payload

    raise ValueError("mapping filename is not allowlisted")


def _load_mappings(mapping_dir: Path, requested_lid: str) -> dict[str, set[str]]:
    if mapping_dir.is_symlink() or not mapping_dir.is_dir():
        raise ValueError("mapping directory is unavailable")

    observations: dict[str, set[str]] = {}
    try:
        entries = sorted(mapping_dir.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise ValueError("mapping directory is unreadable") from exc

    for entry in entries:
        if not entry.name.startswith(_MAPPING_FILE_PREFIX):
            continue
        if not (entry.name.endswith(".json") or entry.name.endswith("_reverse.json")):
            continue
        if not (
            _FORWARD_FILE_RE.fullmatch(entry.name)
            or _REVERSE_FILE_RE.fullmatch(entry.name)
        ):
            continue
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("mapping entry is not a regular file")
        lid, phone = _parse_mapping_file(entry, entry.name)
        observations.setdefault(lid, set()).add(phone)

    return {requested_lid: observations.get(requested_lid, set())}


def resolve_phone(chat_id: str, mapping_dir: Path) -> PhoneResolution:
    """Resolve a trusted WhatsApp chat identifier to one transport phone.

    The caller is responsible for proving that ``chat_id`` came from an
    authorized conversation. This function only validates the identifier and
    the local mapping evidence; it never derives identity from session keys.
    """
    if not isinstance(chat_id, str):
        return _unavailable("PHONE_NOT_RESOLVED")

    direct = _PHONE_JID_RE.fullmatch(chat_id)
    if direct:
        return PhoneResolution("ok", direct.group("phone"), "resolved")

    lid_match = _LID_JID_RE.fullmatch(chat_id)
    if not lid_match:
        return _unavailable("PHONE_NOT_RESOLVED")

    requested_lid = lid_match.group("lid")
    try:
        observations = _load_mappings(Path(mapping_dir), requested_lid)
    except (TypeError, ValueError):
        return _unavailable("PHONE_MAPPING_INVALID")

    phones = observations[requested_lid]
    if not phones:
        return _unavailable("PHONE_MAPPING_UNAVAILABLE")
    if len(phones) != 1:
        return _unavailable("PHONE_IDENTITY_AMBIGUOUS")
    return PhoneResolution("ok", next(iter(phones)), "resolved")
