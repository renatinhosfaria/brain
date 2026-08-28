"""Configuration loaded from a local TOML file and/or process environment.

Secrets are accepted from the environment as raw values only so Hermes' secret
scope can supply them. Persisted configuration stores SHA-256 digests instead.
The service never logs either form.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_STATE_DB = Path("/root/.hermes/state.db")
DEFAULT_KANBAN_DB = Path("/root/.hermes/kanban.db")
DEFAULT_WHATSAPP_SESSION_DIR = Path(
    "/root/.hermes/platforms/whatsapp/session"
)
VALID_MODES = frozenset({"worker", "gateway"})
VALID_TOOLS = frozenset(
    {"conversation_recent", "conversation_search", "conversation_phone"}
)
_PRINCIPAL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
logger = logging.getLogger("brain.config")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_digest(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class PrincipalConfig:
    name: str
    mode: Literal["worker", "gateway"] | str
    token_sha256: str
    tools: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _PRINCIPAL_RE.fullmatch(self.name):
            raise ValueError("principal name is invalid")
        if self.mode not in VALID_MODES:
            raise ValueError("principal mode must be worker or gateway")
        if not _valid_digest(self.token_sha256):
            raise ValueError(f"invalid credential digest for {self.name}")
        normalized_digest = self.token_sha256.lower()
        object.__setattr__(self, "token_sha256", normalized_digest)
        if not isinstance(self.tools, frozenset):
            object.__setattr__(self, "tools", frozenset(self.tools))
        if not self.tools or not self.tools.issubset(VALID_TOOLS):
            raise ValueError(f"invalid tool allowlist for {self.name}")


@dataclass(frozen=True)
class BrainSettings:
    state_db: Path = DEFAULT_STATE_DB
    kanban_db: Path = DEFAULT_KANBAN_DB
    whatsapp_session_dir: Path = DEFAULT_WHATSAPP_SESSION_DIR
    board: str = "default"
    host: str = "127.0.0.1"
    port: int = 8765
    principals: Mapping[str, PrincipalConfig] = field(default_factory=dict)
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
        if not self.principals:
            raise ValueError("at least one Brain principal is required")
        normalized: dict[str, PrincipalConfig] = {}
        for name, principal in self.principals.items():
            if name != principal.name:
                raise ValueError("principal mapping key does not match its name")
            normalized[name] = principal
        gateway_names = [
            principal.name
            for principal in normalized.values()
            if principal.mode == "gateway"
        ]
        if gateway_names != ["default"]:
            raise ValueError("exactly one gateway principal named default is required")
        digests = [principal.token_sha256 for principal in normalized.values()]
        if len(set(digests)) != len(digests):
            raise ValueError("Brain principals must use distinct credentials")
        object.__setattr__(self, "principals", normalized)
        object.__setattr__(self, "state_db", Path(self.state_db))
        object.__setattr__(self, "kanban_db", Path(self.kanban_db))
        object.__setattr__(self, "whatsapp_session_dir", Path(self.whatsapp_session_dir))
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
        principals_raw = raw.get("principals", {})

        principals: dict[str, PrincipalConfig] = {}
        for name in sorted(principals_raw):
            configured = principals_raw.get(name, {})
            if not isinstance(configured, Mapping):
                raise ValueError(f"invalid configuration for principal {name}")
            env_name = re.sub(r"[^A-Za-z0-9]", "_", str(name).upper())
            env_hash = os.environ.get(f"BRAIN_{env_name}_TOKEN_HASH")
            env_raw = os.environ.get(f"BRAIN_{env_name}_TOKEN")
            if name == "default":
                env_hash = env_hash or os.environ.get("BRAIN_GATEWAY_TOKEN_HASH")
                env_raw = env_raw or os.environ.get("BRAIN_GATEWAY_TOKEN")
            digest = env_hash or configured.get("token_sha256")
            if digest is None and env_raw:
                digest = token_digest(env_raw)
            if digest:
                principals[name] = PrincipalConfig(
                    name=str(name),
                    mode=str(configured.get("mode", "")),
                    token_sha256=str(digest),
                    tools=frozenset(str(tool) for tool in configured.get("tools", [])),
                )

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
            whatsapp_session_dir=Path(
                os.environ.get(
                    "BRAIN_WHATSAPP_SESSION_DIR",
                    server.get("whatsapp_session_dir", DEFAULT_WHATSAPP_SESSION_DIR),
                )
            ),
            board=str(
                os.environ.get("BRAIN_KANBAN_BOARD", server.get("board", "default"))
            ),
            host=str(os.environ.get("BRAIN_HOST", server.get("host", "127.0.0.1"))),
            port=int(os.environ.get("BRAIN_PORT", server.get("port", 8765))),
            principals=principals,
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
