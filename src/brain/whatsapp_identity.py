"""Fail-closed WhatsApp JID to transport-phone resolution."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .transport_models import RuntimeIds

_PHONE_JID_RE = re.compile(r"^(?P<phone>[1-9][0-9]{6,14})@s\.whatsapp\.net$")
_LID_JID_RE = re.compile(r"^(?P<lid>[0-9]{1,20})@lid$")
_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
_LID_RE = re.compile(r"^[0-9]{1,20}$")
_FORWARD_FILE_RE = re.compile(r"^lid-mapping-(?P<phone>[1-9][0-9]{6,14})\.json$")
_REVERSE_FILE_RE = re.compile(r"^lid-mapping-(?P<lid>[0-9]{1,20})_reverse\.json$")
_MAPPING_FILE_PREFIX = "lid-mapping-"
_MAX_MAPPING_BYTES = 4096


@dataclass(frozen=True)
class PhoneResolution:
    status: Literal["ok", "unavailable"]
    phone: str | None
    reason: str


@dataclass(frozen=True)
class TransportIdentityResolution:
    status: Literal["ok", "unavailable"]
    phone: str | None
    contact_key: str | None
    reason: str


def _unavailable(reason: str) -> PhoneResolution:
    return PhoneResolution(status="unavailable", phone=None, reason=reason)


def _parse_mapping_file(path: Path, filename: str) -> tuple[str, str]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_MAPPING_BYTES + 1)
        if len(raw) > _MAX_MAPPING_BYTES:
            raise ValueError("mapping file is too large")
        payload = json.loads(raw.decode("utf-8"))
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
    invalid_candidate = False
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
        reverse = _REVERSE_FILE_RE.fullmatch(entry.name)
        requested_reverse = bool(reverse and reverse.group("lid") == requested_lid)
        if entry.is_symlink() or not entry.is_file():
            if requested_reverse:
                raise ValueError("requested mapping entry is not a regular file")
            invalid_candidate = True
            continue
        try:
            lid, phone = _parse_mapping_file(entry, entry.name)
        except (TypeError, ValueError):
            if requested_reverse:
                raise
            invalid_candidate = True
            continue
        observations.setdefault(lid, set()).add(phone)

    if not observations.get(requested_lid) and invalid_candidate:
        raise ValueError("no valid evidence for requested mapping")
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


def same_verified_contact(chat_ids: Iterable[str], mapping_dir: Path) -> bool:
    """Return whether longitudinal WhatsApp IDs have one proven contact.

    An exact repeated identifier is already the same database identity. Any
    different identifier must independently resolve to the same verified
    transport phone; a shared Hermes ``session_key`` is never sufficient.
    """
    unique_ids = tuple(dict.fromkeys(chat_ids))
    if not unique_ids:
        return False
    if any(not isinstance(chat_id, str) or not chat_id for chat_id in unique_ids):
        return False
    if len(unique_ids) == 1:
        return True

    resolutions = [resolve_phone(chat_id, mapping_dir) for chat_id in unique_ids]
    if any(resolution.status != "ok" for resolution in resolutions):
        return False
    phones = {resolution.phone for resolution in resolutions}
    return len(phones) == 1


def _transport_unavailable(reason: str) -> TransportIdentityResolution:
    return TransportIdentityResolution(
        status="unavailable", phone=None, contact_key=None, reason=reason
    )


def _transport_mapping_pairs(mapping_dir: Path) -> set[tuple[str, str]]:
    """Read only allowlisted observer mappings for transport identity proof."""
    path = Path(mapping_dir)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("observer mapping directory is unavailable")
    pairs: set[tuple[str, str]] = set()
    try:
        entries = sorted(path.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise ValueError("observer mapping directory is unreadable") from exc

    for entry in entries:
        if not entry.name.startswith(_MAPPING_FILE_PREFIX):
            continue
        if not (
            _FORWARD_FILE_RE.fullmatch(entry.name)
            or _REVERSE_FILE_RE.fullmatch(entry.name)
        ):
            raise ValueError("observer mapping filename is not allowlisted")
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("observer mapping evidence is not a regular file")
        try:
            lid, phone = _parse_mapping_file(entry, entry.name)
        except (TypeError, ValueError) as exc:
            raise ValueError("observer mapping evidence is invalid") from exc
        pairs.add((lid, phone))
    return pairs


def verify_transport_identity(
    *,
    remote_jid_hmac: str,
    contact_key: str | None,
    mapping_dir: Path,
    transport_ids: RuntimeIds,
) -> TransportIdentityResolution:
    """Prove one canonical phone from observer mapping evidence and HMACs.

    The raw JIDs are constructed only in memory from allowlisted mapping
    evidence. No caller-provided JID, display name, or arbitrary JSON is used.
    """
    if not isinstance(remote_jid_hmac, str) or not re.fullmatch(
        r"[0-9a-f]{64}", remote_jid_hmac
    ):
        return _transport_unavailable("REMOTE_JID_HMAC_INVALID")
    if contact_key is not None and not re.fullmatch(r"[0-9a-f]{64}", contact_key):
        return _transport_unavailable("CONTACT_KEY_INVALID")

    try:
        pairs = _transport_mapping_pairs(Path(mapping_dir))
    except (OSError, TypeError, ValueError):
        return _transport_unavailable("IDENTITY_UNAVAILABLE")

    matching_phones: set[str] = set()
    for lid, phone in pairs:
        candidates = (f"{phone}@s.whatsapp.net", f"{lid}@lid")
        if any(
            hmac.compare_digest(remote_jid_hmac, transport_ids.jid_hmac(candidate))
            for candidate in candidates
        ):
            matching_phones.add(phone)

    if not matching_phones:
        return _transport_unavailable("IDENTITY_UNAVAILABLE")
    if len(matching_phones) != 1:
        return _transport_unavailable("IDENTITY_AMBIGUOUS")

    phone = next(iter(matching_phones))
    derived_contact_key = transport_ids.contact_key(phone)
    if contact_key is not None and not hmac.compare_digest(
        contact_key, derived_contact_key
    ):
        return _transport_unavailable("CONTACT_KEY_MISMATCH")
    return TransportIdentityResolution(
        status="ok",
        phone=phone,
        contact_key=derived_contact_key,
        reason="resolved",
    )


def phone_for_contact_key(
    contact_key: str, mapping_dir: Path, transport_ids: object
) -> str | None:
    """Recover the canonical phone behind a contact key, transiently.

    A caller must compare its expectation against the live FamaChat
    record, and a HMAC cannot be compared to a phone number. So the phone is
    resolved at claim time from mapping evidence Brain already trusts, handed
    over for that one comparison, and never stored by either service
    (spec sections 6.1 and 13).

    Exactly one mapped phone may match. Zero means the evidence is gone;
    several would mean the key is ambiguous, and guessing which lead a status
    belongs to is the one mistake this design exists to prevent.
    """
    if not isinstance(contact_key, str) or not contact_key:
        return None
    try:
        pairs = _transport_mapping_pairs(Path(mapping_dir))
    except ValueError:
        return None

    matches = {
        phone
        for _lid, phone in pairs
        if _PHONE_RE.fullmatch(phone)
        and hmac.compare_digest(transport_ids.contact_key(phone), contact_key)
    }
    if len(matches) != 1:
        return None
    return matches.pop()
