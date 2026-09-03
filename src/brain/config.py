"""Configuration loaded from a local TOML file and/or process environment.

Secrets are accepted from the environment as raw values only so Hermes' secret
scope can supply them. Persisted configuration stores SHA-256 digests instead.
The service never logs either form.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from brain.meta_ads_models import canonical_account_id

DEFAULT_STATE_DB = Path("/root/.hermes/state.db")
DEFAULT_KANBAN_DB = Path("/root/.hermes/kanban.db")
DEFAULT_WHATSAPP_SESSION_DIR = Path("/root/.hermes/platforms/whatsapp/session")
DEFAULT_RUNTIME_DB = Path("/var/lib/brain/runtime/brain-runtime.db")
DEFAULT_OBSERVER_SESSION_DIR = Path("/var/lib/brain/whatsapp-observer/session")
DEFAULT_META_ADS_OAUTH_STORE = Path(
    "/var/lib/brain/credentials/meta-ads-oauth.json.enc"
)
DEFAULT_META_ADS_OAUTH_KEY = Path("/etc/brain/meta-ads-oauth.key")
DEFAULT_META_ADS_OAUTH_REDIRECT_URI = "http://127.0.0.1:8766/oauth/callback"
VALID_MODES = frozenset({"worker", "gateway", "service"})
VALID_TOOLS = frozenset(
    {
        "conversation_recent",
        "conversation_search",
        "conversation_phone",
        "conversation_context",
        "transport_ingest",
    }
)
_PRINCIPAL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DECIMAL_INTEGER_RE = re.compile(r"^-?[0-9]+$")
logger = logging.getLogger("brain.config")
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _meta_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in {"true", "false"}:
        return value == "true"
    raise ValueError(f"{name} must be true or false")


def _meta_expiry(value: object, name: str) -> float | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be RFC3339")
    if not _RFC3339_UTC_RE.fullmatch(value):
        raise ValueError(f"{name} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.timestamp()


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _visible_token(value: str, maximum: int) -> bool:
    """Match the observer identity shape the transport boundary already accepts."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def _valid_digest(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _observer_device_ids(server: Mapping[str, object]) -> tuple[str, ...]:
    """Read the expected observer identities from env or persisted config."""
    raw = os.environ.get("BRAIN_OBSERVER_DEVICE_IDS")
    if raw is not None:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    configured = server.get("observer_device_ids", ())
    if isinstance(configured, str):
        return tuple(part.strip() for part in configured.split(",") if part.strip())
    return tuple(str(value).strip() for value in configured if str(value).strip())


def _raw_attribution_limit(
    server: Mapping[str, object], key: str, environment_name: str, default: int
) -> int:
    """Read an integer raw-attribution limit without coercing TOML types."""
    environment_value = os.environ.get(environment_name)
    if environment_value is not None:
        if not _DECIMAL_INTEGER_RE.fullmatch(environment_value):
            raise ValueError(f"{environment_name} must be a decimal integer")
        return int(environment_value)
    configured = server.get(key, default)
    if isinstance(configured, bool) or not isinstance(configured, int):
        raise TypeError(f"{key} must be an integer")
    return configured


@dataclass(frozen=True)
class PrincipalConfig:
    name: str
    mode: Literal["worker", "gateway", "service"] | str
    token_sha256: str
    tools: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _PRINCIPAL_RE.fullmatch(self.name):
            raise ValueError("principal name is invalid")
        if self.mode not in VALID_MODES:
            raise ValueError("principal mode must be worker, gateway, or service")
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
    runtime_db: Path = DEFAULT_RUNTIME_DB
    observer_session_dir: Path = DEFAULT_OBSERVER_SESSION_DIR
    observer_device_ids: tuple[str, ...] = ()
    board: str = "default"
    host: str = "127.0.0.1"
    port: int = 8765
    principals: Mapping[str, PrincipalConfig] = field(default_factory=dict)
    cursor_secret: bytes = b""
    transport_hmac_secret: bytes = b""
    transport_retention_days: int = 90
    display_name_ttl_hours: int = 24
    history_budget_chars: int = 12_000
    message_max_chars: int = 2_000
    ctwa_raw_max_bytes: int = 4 * 1024 * 1024
    ctwa_raw_max_depth: int = 32
    ctwa_raw_max_nodes: int = 10_000
    context_response_max_bytes: int = 32 * 1024 * 1024
    busy_retries: int = 2
    busy_timeout_seconds: float = 1.0
    meta_attribution_enabled: bool = False
    meta_ad_account_id: str = ""
    meta_ads_mcp_auth_mode: Literal["token", "oauth"] = "token"
    meta_ads_mcp_access_token: str = field(default="", repr=False)
    meta_ads_mcp_token_expires_at: float | None = None
    meta_ads_oauth_store_path: Path = DEFAULT_META_ADS_OAUTH_STORE
    meta_ads_oauth_key_path: Path = DEFAULT_META_ADS_OAUTH_KEY
    meta_ads_oauth_redirect_uri: str = DEFAULT_META_ADS_OAUTH_REDIRECT_URI
    meta_ads_mcp_timeout_seconds: float = 4.0
    meta_ads_mcp_response_max_bytes: int = 8 * 1024 * 1024
    meta_ads_sync_interval_seconds: int = 900
    meta_ads_full_sync_interval_seconds: int = 86_400

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
        for value in (
            self.ctwa_raw_max_bytes,
            self.ctwa_raw_max_depth,
            self.ctwa_raw_max_nodes,
            self.context_response_max_bytes,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("raw attribution limits must be positive")
        if self.context_response_max_bytes < self.ctwa_raw_max_bytes:
            raise ValueError("context response maximum must cover raw attribution")
        if self.busy_retries < 0 or self.busy_retries > 5:
            raise ValueError("busy_retries must be between 0 and 5")
        if self.busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        if not isinstance(self.meta_attribution_enabled, bool):
            raise TypeError("meta_attribution_enabled must be boolean")
        if self.meta_attribution_enabled or self.meta_ad_account_id:
            canonical_account_id(self.meta_ad_account_id)
        if not isinstance(self.meta_ads_mcp_access_token, str):
            raise TypeError("Meta Ads token must be text")
        if self.meta_ads_mcp_auth_mode not in {"token", "oauth"}:
            raise ValueError("Meta Ads authentication mode must be token or oauth")
        if self.meta_ads_oauth_redirect_uri != DEFAULT_META_ADS_OAUTH_REDIRECT_URI:
            raise ValueError("Meta Ads OAuth redirect URI is fixed")
        object.__setattr__(
            self, "meta_ads_oauth_store_path", Path(self.meta_ads_oauth_store_path)
        )
        object.__setattr__(
            self, "meta_ads_oauth_key_path", Path(self.meta_ads_oauth_key_path)
        )
        if self.meta_ads_mcp_token_expires_at is not None and (
            not isinstance(self.meta_ads_mcp_token_expires_at, (int, float))
            or isinstance(self.meta_ads_mcp_token_expires_at, bool)
            or not math.isfinite(self.meta_ads_mcp_token_expires_at)
        ):
            raise ValueError("Meta Ads token expiry must be finite")
        if (
            not isinstance(self.meta_ads_mcp_timeout_seconds, (int, float))
            or isinstance(self.meta_ads_mcp_timeout_seconds, bool)
            or not math.isfinite(self.meta_ads_mcp_timeout_seconds)
            or self.meta_ads_mcp_timeout_seconds <= 0
        ):
            raise ValueError("Meta Ads timeout must be finite and positive")
        if (
            isinstance(self.meta_ads_mcp_response_max_bytes, bool)
            or not isinstance(self.meta_ads_mcp_response_max_bytes, int)
            or not 1 * 1024 * 1024
            <= self.meta_ads_mcp_response_max_bytes
            <= 32 * 1024 * 1024
        ):
            raise ValueError("Meta Ads response limit must be between 1 and 32 MiB")
        if (
            isinstance(self.meta_ads_sync_interval_seconds, bool)
            or not isinstance(self.meta_ads_sync_interval_seconds, int)
            or self.meta_ads_sync_interval_seconds < 60
        ):
            raise ValueError("Meta Ads sync interval must be at least 60 seconds")
        if (
            isinstance(self.meta_ads_full_sync_interval_seconds, bool)
            or not isinstance(self.meta_ads_full_sync_interval_seconds, int)
            or self.meta_ads_full_sync_interval_seconds
            < self.meta_ads_sync_interval_seconds
        ):
            raise ValueError("Meta Ads full sync interval must cover incremental sync")
        if self.transport_retention_days <= 0:
            raise ValueError("transport_retention_days must be positive")
        if self.display_name_ttl_hours <= 0:
            raise ValueError("display_name_ttl_hours must be positive")
        # Transport ingestion derives event IDs from these identities, so a
        # fresh deployment can resolve its first message before any transport
        # event exists to discover the device from.
        device_ids = tuple(sorted({str(value) for value in self.observer_device_ids}))
        for device_id in device_ids:
            if not _visible_token(device_id, 128):
                raise ValueError("observer_device_ids contains an invalid identity")
        object.__setattr__(self, "observer_device_ids", device_ids)
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
        object.__setattr__(
            self, "whatsapp_session_dir", Path(self.whatsapp_session_dir)
        )
        object.__setattr__(self, "runtime_db", Path(self.runtime_db))
        object.__setattr__(
            self, "observer_session_dir", Path(self.observer_session_dir)
        )
        if not self.cursor_secret:
            logger.warning(
                "BRAIN_CURSOR_SECRET is not configured; generated an ephemeral "
                "cursor secret. Cursors do not survive restart, and Brain must "
                "run as a single process."
            )
            object.__setattr__(self, "cursor_secret", secrets.token_bytes(32))
        if len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        if self.transport_hmac_secret and len(self.transport_hmac_secret) < 32:
            raise ValueError("transport_hmac_secret must contain at least 32 bytes")

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
                raise TypeError(f"invalid configuration for principal {name}")
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

        def required_hmac_secret(name: str) -> bytes:
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"{name} is required")
            parsed = (
                bytes.fromhex(value) if _valid_digest(value) else value.encode("utf-8")
            )
            if len(parsed) < 32:
                raise ValueError(f"{name} must contain at least 32 bytes")
            return parsed

        transport_hmac_secret = required_hmac_secret("BRAIN_TRANSPORT_HMAC_SECRET")
        meta_enabled = _meta_bool(
            os.environ.get(
                "BRAIN_META_ATTRIBUTION_ENABLED",
                server.get("meta_attribution_enabled", False),
            ),
            "BRAIN_META_ATTRIBUTION_ENABLED",
        )
        configured_account = os.environ.get(
            "BRAIN_META_AD_ACCOUNT_ID", server.get("meta_ad_account_id", "")
        )
        if configured_account:
            canonical_account_id(configured_account)
        meta_account = canonical_account_id(configured_account) if meta_enabled else ""
        meta_token = os.environ.get("BRAIN_META_ADS_MCP_ACCESS_TOKEN", "")
        meta_auth_mode = str(
            os.environ.get(
                "BRAIN_META_ADS_MCP_AUTH_MODE",
                server.get("meta_ads_mcp_auth_mode", "token"),
            )
        )
        if meta_auth_mode == "oauth":
            # OAuth intentionally never falls back to this legacy environment
            # credential.  It remains available only when token mode is selected.
            meta_token = ""
        meta_expiry = _meta_expiry(
            os.environ.get(
                "BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT",
                server.get("meta_ads_mcp_token_expires_at"),
            ),
            "BRAIN_META_ADS_MCP_TOKEN_EXPIRES_AT",
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
            runtime_db=Path(
                os.environ.get(
                    "BRAIN_RUNTIME_DB", server.get("runtime_db", DEFAULT_RUNTIME_DB)
                )
            ),
            observer_session_dir=Path(
                os.environ.get(
                    "BRAIN_OBSERVER_SESSION_DIR",
                    server.get("observer_session_dir", DEFAULT_OBSERVER_SESSION_DIR),
                )
            ),
            observer_device_ids=_observer_device_ids(server),
            board=str(
                os.environ.get("BRAIN_KANBAN_BOARD", server.get("board", "default"))
            ),
            host=str(os.environ.get("BRAIN_HOST", server.get("host", "127.0.0.1"))),
            port=int(os.environ.get("BRAIN_PORT", server.get("port", 8765))),
            principals=principals,
            cursor_secret=cursor_secret,
            transport_hmac_secret=transport_hmac_secret,
            transport_retention_days=int(
                os.environ.get(
                    "BRAIN_TRANSPORT_RETENTION_DAYS",
                    server.get("transport_retention_days", 90),
                )
            ),
            display_name_ttl_hours=int(
                os.environ.get(
                    "BRAIN_DISPLAY_NAME_TTL_HOURS",
                    server.get("display_name_ttl_hours", 24),
                )
            ),
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
            ctwa_raw_max_bytes=_raw_attribution_limit(
                server,
                "ctwa_raw_max_bytes",
                "BRAIN_CTWA_RAW_MAX_BYTES",
                4 * 1024 * 1024,
            ),
            ctwa_raw_max_depth=_raw_attribution_limit(
                server, "ctwa_raw_max_depth", "BRAIN_CTWA_RAW_MAX_DEPTH", 32
            ),
            ctwa_raw_max_nodes=_raw_attribution_limit(
                server, "ctwa_raw_max_nodes", "BRAIN_CTWA_RAW_MAX_NODES", 10_000
            ),
            context_response_max_bytes=_raw_attribution_limit(
                server,
                "context_response_max_bytes",
                "BRAIN_CONTEXT_RESPONSE_MAX_BYTES",
                32 * 1024 * 1024,
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
            meta_attribution_enabled=meta_enabled,
            meta_ad_account_id=meta_account,
            meta_ads_mcp_auth_mode=meta_auth_mode,  # type: ignore[arg-type]
            meta_ads_mcp_access_token=meta_token,
            meta_ads_mcp_token_expires_at=meta_expiry,
            meta_ads_oauth_store_path=Path(
                os.environ.get(
                    "BRAIN_META_ADS_OAUTH_STORE",
                    server.get(
                        "meta_ads_oauth_store_path", DEFAULT_META_ADS_OAUTH_STORE
                    ),
                )
            ),
            meta_ads_oauth_key_path=Path(
                os.environ.get(
                    "BRAIN_META_ADS_OAUTH_KEY_FILE",
                    server.get("meta_ads_oauth_key_path", DEFAULT_META_ADS_OAUTH_KEY),
                )
            ),
            meta_ads_oauth_redirect_uri=str(
                os.environ.get(
                    "BRAIN_META_ADS_OAUTH_REDIRECT_URI",
                    server.get(
                        "meta_ads_oauth_redirect_uri",
                        DEFAULT_META_ADS_OAUTH_REDIRECT_URI,
                    ),
                )
            ),
            meta_ads_mcp_timeout_seconds=float(
                os.environ.get(
                    "BRAIN_META_ADS_MCP_TIMEOUT_SECONDS",
                    server.get("meta_ads_mcp_timeout_seconds", 4.0),
                )
            ),
            meta_ads_mcp_response_max_bytes=_raw_attribution_limit(
                server,
                "meta_ads_mcp_response_max_bytes",
                "BRAIN_META_ADS_MCP_RESPONSE_MAX_BYTES",
                8 * 1024 * 1024,
            ),
            meta_ads_sync_interval_seconds=int(
                os.environ.get(
                    "BRAIN_META_ADS_SYNC_INTERVAL_SECONDS",
                    server.get("meta_ads_sync_interval_seconds", 900),
                )
            ),
            meta_ads_full_sync_interval_seconds=int(
                os.environ.get(
                    "BRAIN_META_ADS_FULL_SYNC_INTERVAL_SECONDS",
                    server.get("meta_ads_full_sync_interval_seconds", 86_400),
                )
            ),
        )
