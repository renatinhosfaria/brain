"""Configuration loaded from a local TOML file and/or process environment.

Secrets are accepted from the environment as raw values only so Hermes' secret
scope can supply them. Persisted configuration stores SHA-256 digests instead.
The service never logs either form.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_PROFILES = frozenset({"reno", "famaagent"})
DEFAULT_STATE_DB = Path("/root/.hermes/state.db")
DEFAULT_KANBAN_DB = Path("/root/.hermes/kanban.db")
logger = logging.getLogger("brain.config")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_digest(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class BrainSettings:
    state_db: Path = DEFAULT_STATE_DB
    kanban_db: Path = DEFAULT_KANBAN_DB
    board: str = "default"
    host: str = "127.0.0.1"
    port: int = 8765
    credentials: Mapping[str, str] = field(default_factory=dict)
    cursor_secret: bytes = b""
    history_budget_chars: int = 12_000
    message_max_chars: int = 2_000
    busy_retries: int = 2
    busy_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Brain V1 must bind to localhost")
        if self.board != "default":
            raise ValueError("Brain V1 supports only the default Kanban board")
        if not (1 <= self.port <= 65535):
            raise ValueError("port must be between 1 and 65535")
        if self.history_budget_chars < 256:
            raise ValueError("history budget is too small")
        if self.message_max_chars < 64:
            raise ValueError("message limit is too small")
        if self.busy_retries < 0 or self.busy_retries > 5:
            raise ValueError("busy_retries must be between 0 and 5")
        if self.busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        if not self.credentials:
            raise ValueError("reno and famaagent credentials are required")
        if set(self.credentials) != ALLOWED_PROFILES:
            raise ValueError("V1 credentials must contain exactly reno and famaagent")
        digests = []
        normalized: dict[str, str] = {}
        for profile, digest in self.credentials.items():
            if (
                profile not in ALLOWED_PROFILES
                or not isinstance(digest, str)
                or not _valid_digest(digest)
            ):
                raise ValueError(f"invalid credential digest for {profile}")
            normalized_digest = digest.lower()
            normalized[profile] = normalized_digest
            digests.append(normalized_digest)
        if digests[0] == digests[1]:
            raise ValueError("Reno and FamaAgent must use distinct credentials")
        object.__setattr__(self, "credentials", normalized)
        if not self.cursor_secret:
            logger.warning(
                "BRAIN_CURSOR_SECRET is not configured; generated an ephemeral "
                "cursor secret. Cursors do not survive restart, and Brain must "
                "run as a single process."
            )
            object.__setattr__(self, "cursor_secret", secrets.token_bytes(32))
        if len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")

    @classmethod
    def from_env(cls, config_path: Path | None = None) -> BrainSettings:
        """Load production configuration without reading Hermes secrets files."""
        raw: dict = {}
        selected = config_path or (
            Path(os.environ["BRAIN_CONFIG"]) if os.environ.get("BRAIN_CONFIG") else None
        )
        if selected and selected.exists():
            with selected.open("rb") as handle:
                raw = tomllib.load(handle)

        server = raw.get("server", {})
        profiles = raw.get("profiles", {})

        credentials: dict[str, str] = {}
        for profile in sorted(ALLOWED_PROFILES):
            env_hash = os.environ.get(f"BRAIN_{profile.upper()}_TOKEN_HASH")
            env_raw = os.environ.get(f"BRAIN_{profile.upper()}_TOKEN")
            configured = profiles.get(profile, {})
            digest = env_hash or configured.get("token_sha256")
            if digest is None and env_raw:
                digest = token_digest(env_raw)
            if digest:
                credentials[profile] = str(digest).lower()

        cursor_value = os.environ.get("BRAIN_CURSOR_SECRET") or server.get(
            "cursor_secret"
        )
        cursor_secret = (
            bytes.fromhex(cursor_value)
            if cursor_value and _valid_digest(cursor_value)
            else (cursor_value.encode("utf-8") if cursor_value else b"")
        )

        return cls(
            state_db=Path(
                os.environ.get(
                    "BRAIN_STATE_DB", server.get("state_db", DEFAULT_STATE_DB)
                )
            ),
            kanban_db=Path(
                os.environ.get(
                    "BRAIN_KANBAN_DB", server.get("kanban_db", DEFAULT_KANBAN_DB)
                )
            ),
            board=str(
                os.environ.get("BRAIN_KANBAN_BOARD", server.get("board", "default"))
            ),
            host=str(os.environ.get("BRAIN_HOST", server.get("host", "127.0.0.1"))),
            port=int(os.environ.get("BRAIN_PORT", server.get("port", 8765))),
            credentials=credentials,
            cursor_secret=cursor_secret,
            history_budget_chars=int(
                os.environ.get(
                    "BRAIN_HISTORY_BUDGET_CHARS",
                    server.get("history_budget_chars", 12_000),
                )
            ),
            message_max_chars=int(
                os.environ.get(
                    "BRAIN_MESSAGE_MAX_CHARS", server.get("message_max_chars", 2_000)
                )
            ),
            busy_retries=int(
                os.environ.get("BRAIN_BUSY_RETRIES", server.get("busy_retries", 2))
            ),
            busy_timeout_seconds=float(
                os.environ.get(
                    "BRAIN_BUSY_TIMEOUT_SECONDS",
                    server.get("busy_timeout_seconds", 1.0),
                )
            ),
        )
