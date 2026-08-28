"""Domain errors and the deliberately small wire-level error vocabulary."""

from __future__ import annotations

from dataclasses import dataclass

GENERIC_DENY = "Brain access denied for this execution context."
GENERIC_UNAVAILABLE = (
    "Brain is temporarily unavailable; historical context could not be verified."
)
GENERIC_REQUEST = "Brain request could not be processed."


@dataclass(frozen=True)
class BrainError(Exception):
    """An internal error that must not be exposed verbatim to the worker."""

    code: str
    unavailable: bool = False

    @property
    def public_message(self) -> str:
        if self.unavailable:
            return GENERIC_UNAVAILABLE
        if self.code in {"SEARCH_INVALID", "GATEWAY_REQUEST_INVALID"}:
            return GENERIC_REQUEST
        return GENERIC_DENY


class DatabaseUnavailable(BrainError):
    def __init__(self) -> None:
        super().__init__("DB_UNAVAILABLE", unavailable=True)


class SchemaIncompatible(BrainError):
    def __init__(self) -> None:
        super().__init__("DB_SCHEMA_INCOMPATIBLE", unavailable=True)


class InvalidRequest(BrainError):
    def __init__(self) -> None:
        super().__init__("SEARCH_INVALID")
