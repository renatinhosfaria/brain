"""Small, secret-safe OAuth client for Meta's hosted Ads MCP server.

The module deliberately keeps the OAuth browser interaction out of the Brain
service: a short-lived local callback helper is used by an operator-facing CLI
instead.  Runtime callers only load and refresh the encrypted credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import anyio
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

META_ADS_MCP_RESOURCE = "https://mcp.facebook.com/ads"
OAUTH_METADATA_URL = (
    "https://mcp.facebook.com/.well-known/oauth-authorization-server/ads"
)
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8766/oauth/callback"
DEFAULT_STORE_PATH = Path("/var/lib/brain/credentials/meta-ads-oauth.json.enc")
DEFAULT_KEY_PATH = Path("/etc/brain/meta-ads-oauth.key")
ALLOWED_HOSTS = frozenset(
    {"mcp.facebook.com", "www.facebook.com", "graph.facebook.com"}
)
ALLOWED_SCOPES = frozenset({"ads_read", "ads_mcp_management"})
_AAD = b"brain-meta-ads-oauth-v1"
_MAX_ENVELOPE_BYTES = 64 * 1024
_MAX_KEY_BYTES = 64
_MAX_HTTP_JSON_BYTES = 64 * 1024
_DYNAMIC_CLIENT_NAME = "Brain Meta Ads MCP"
_MAX_DYNAMIC_CLIENT_NAME_BYTES = 128
_ENVELOPE_FIELDS = frozenset({"version", "nonce", "ciphertext"})
_CREDENTIAL_FIELDS = frozenset(
    {
        "version",
        "client_id",
        "client_secret",
        "access_token",
        "refresh_token",
        "access_expires_at",
        "refresh_expires_at",
        "scopes",
        "issuer",
        "resource",
        "created_at",
        "updated_at",
    }
)
_CLIENT_CONFIGURATION_FIELDS = frozenset(
    {"version", "kind", "client_id", "client_secret", "configured_at"}
)
_DYNAMIC_CLIENT_FIELDS = frozenset(
    {
        "version",
        "client_id",
        "client_secret",
        "registration_access_token",
        "registered_at",
        "expires_at",
        "issuer",
        "resource",
        "redirect_uri",
        "scopes",
    }
)
_STORE_PAYLOAD_FIELDS = frozenset({"version", "dynamic_client", "credentials"})

MetadataFetcher = Callable[[str], object]
TokenRequester = Callable[[str, dict[str, str]], object]
RegistrationRequester = Callable[[str, dict[str, object]], object]
CredentialState = Literal["missing", "ready", "expiring", "expired", "degraded"]
RegistrationState = Literal["missing", "ready", "expired", "degraded"]


class _NoRedirect(HTTPRedirectHandler):
    """Treat every redirect as a failed OAuth request before sending it onward."""

    def redirect_request(
        self,
        _request: Request,
        _file_pointer: object,
        _code: int,
        _message: str,
        _headers: object,
        _new_url: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect())


class OAuthError(Exception):
    """A bounded error vocabulary which never embeds server-provided text."""

    _CODES = frozenset(
        {
            "oauth_metadata_invalid",
            "oauth_metadata_unavailable",
            "oauth_registration_invalid",
            "oauth_registration_unavailable",
            "oauth_callback_invalid",
            "oauth_token_invalid",
            "oauth_invalid_grant",
            "oauth_token_unavailable",
            "oauth_credentials_invalid",
            "oauth_credentials_unavailable",
            "oauth_legacy_store",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            code = "oauth_token_unavailable"
        super().__init__(code)


@dataclass(frozen=True)
class OAuthMetadata:
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scopes_supported: frozenset[str]
    authorization_response_iss_parameter_supported: bool = False


@dataclass(frozen=True)
class OAuthDynamicClient:
    """Validated public client registration returned by the OAuth server."""

    client_id: str
    client_secret: str | None = field(repr=False)
    registration_access_token: str | None = field(repr=False)
    registered_at: float
    expires_at: float | None
    issuer: str
    resource: str
    redirect_uri: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        try:
            normalized_scopes = frozenset(self.scopes)
        except (TypeError, ValueError):
            raise ValueError("OAuth dynamic client is invalid") from None
        object.__setattr__(self, "scopes", normalized_scopes)
        if (
            not _safe_secret_text(self.client_id)
            or (
                self.client_secret is not None
                and not _safe_secret_text(self.client_secret)
            )
            or (
                self.registration_access_token is not None
                and not _safe_secret_text(self.registration_access_token)
            )
            or not _finite_number(self.registered_at)
            or self.registered_at <= 0
            or (
                self.expires_at is not None
                and (not _finite_number(self.expires_at) or self.expires_at <= 0)
            )
            or self.issuer != META_ADS_MCP_RESOURCE
            or self.resource != META_ADS_MCP_RESOURCE
            or self.redirect_uri != DEFAULT_REDIRECT_URI
            or self.scopes != frozenset({"ads_read"})
        ):
            raise ValueError("OAuth dynamic client is invalid")


@dataclass(frozen=True)
class OAuthAuthorizationRequest:
    url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    requested_scopes: frozenset[str]
    expected_issuer: str = field(default=META_ADS_MCP_RESOURCE, repr=False)
    require_issuer: bool = field(default=False, repr=False)


@dataclass(frozen=True)
class OAuthCredentials:
    version: int = field(default=1, repr=False, init=False)
    client_id: str
    client_secret: str | None = field(repr=False)
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: float
    refresh_expires_at: float | None
    scopes: frozenset[str]
    issuer: str
    resource: str
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _validate_credentials(self)

    def status(self, now: float | None = None) -> CredentialState:
        current = time.time() if now is None else now
        if not _finite_number(current):
            return "degraded"
        if self.refresh_expires_at is not None and self.refresh_expires_at <= current:
            return "expired"
        if self.access_expires_at <= current:
            return "expiring"
        if self.access_expires_at - current <= 600:
            return "expiring"
        return "ready"


@dataclass(frozen=True)
class OAuthClientConfiguration:
    """Encrypted app registration data present before the first browser login."""

    client_id: str
    client_secret: str | None = field(repr=False)
    configured_at: float

    def __post_init__(self) -> None:
        if (
            not _safe_secret_text(self.client_id)
            or (
                self.client_secret is not None
                and not _safe_secret_text(self.client_secret)
            )
            or not _finite_number(self.configured_at)
        ):
            raise ValueError("OAuth client configuration is invalid")


class OAuthCallback:
    """Consumes exactly one authorization callback for one PKCE request."""

    def __init__(self, request: OAuthAuthorizationRequest) -> None:
        self._request = request
        self._consumed = False

    def consume(self, query: Mapping[str, object]) -> str:
        if self._consumed:
            raise OAuthError("oauth_callback_invalid")
        state = _single_query_value(query.get("state"))
        code = _single_query_value(query.get("code"))
        if (
            state is None
            or code is None
            or (
                self._request.require_issuer
                and (
                    _single_query_value(query.get("iss"))
                    != self._request.expected_issuer
                )
            )
            or not secrets.compare_digest(state, self._request.state)
        ):
            raise OAuthError("oauth_callback_invalid")
        self._consumed = True
        return code

    def serve_once(self, timeout_seconds: float = 300.0) -> str:
        """Receive one callback on the fixed localhost redirect URI."""
        if not _finite_number(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("callback timeout must be positive")
        callback = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/oauth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query: dict[str, object] = parse_qs(
                    parsed.query, keep_blank_values=True
                )
                try:
                    self.server.oauth_code = callback.consume(query)  # type: ignore[attr-defined]
                except OAuthError:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization callback rejected.")
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(
                        b"Authorization received. You may close this page."
                    )

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            with HTTPServer(("127.0.0.1", 8766), _Handler) as server:
                deadline = time.monotonic() + timeout_seconds
                code: object = None
                while code is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    server.timeout = remaining
                    server.handle_request()
                    code = getattr(server, "oauth_code", None)
        except OSError:
            raise OAuthError("oauth_callback_invalid") from None
        if not isinstance(code, str):
            raise OAuthError("oauth_callback_invalid")
        return code


class OAuthCredentialProvider:
    """Lazily loads and refreshes one encrypted OAuth credential set.

    The provider performs no I/O or network work until an MCP session asks for
    a bearer token.  A process-local lock prevents simultaneous refreshes from
    rotating the same refresh token twice.
    """

    def __init__(self, oauth: MetaAdsOAuth) -> None:
        self._oauth = oauth
        self._credentials: OAuthCredentials | None = None
        self._state: CredentialState = "missing"
        self._force_refresh = False
        self._invalidated_access_token: str | None = None
        self._store_marker: tuple[int, int, int] | None = None
        self._lock = threading.RLock()

    def access_token(self, now: float) -> str:
        with self._lock:
            credentials = self._load()
            registration_state = self._oauth.registration_state(now)
            if registration_state != "ready":
                self._state = registration_state
                raise OAuthError("oauth_credentials_unavailable")
            if credentials is None:
                raise OAuthError("oauth_credentials_unavailable")
            if (
                credentials.refresh_expires_at is not None
                and credentials.refresh_expires_at <= now
            ):
                self._state = "expired"
                raise OAuthError("oauth_credentials_unavailable")
            if self._force_refresh or credentials.access_expires_at - now <= 600:
                try:
                    credentials = self._oauth.refresh(credentials)
                    self._oauth.save_credentials(credentials)
                except OAuthError as exc:
                    if exc.args and exc.args[0] == "oauth_invalid_grant":
                        self._credentials = None
                        self._store_marker = self._current_store_marker()
                        self._force_refresh = False
                        self._state = "missing"
                    else:
                        self._state = "degraded"
                    raise
                self._credentials = credentials
                self._force_refresh = False
                self._invalidated_access_token = None
                self._store_marker = self._current_store_marker()
            self._state = credentials.status(now)
            if self._state in {"expired", "degraded"}:
                raise OAuthError("oauth_credentials_unavailable")
            return credentials.access_token

    async def access_token_async(self, now: float, budget_seconds: float) -> str:
        """Refresh in a worker thread without holding the MCP event loop hostage."""
        if not _finite_number(budget_seconds) or budget_seconds <= 0:
            raise TimeoutError
        with anyio.fail_after(budget_seconds):
            return await anyio.to_thread.run_sync(
                self.access_token, now, abandon_on_cancel=True
            )

    def invalidate(self) -> None:
        with self._lock:
            self._invalidated_access_token = (
                None if self._credentials is None else self._credentials.access_token
            )
            self._credentials = None
            self._store_marker = None
            self._force_refresh = True

    def status(self, now: float) -> CredentialState:
        with self._lock:
            credentials = self._load()
            registration_state = self._oauth.registration_state(now)
            if registration_state != "ready":
                self._state = registration_state
                return self._state
            if credentials is None:
                return self._state
            if self._state == "degraded":
                return "degraded"
            self._state = credentials.status(now)
            return self._state

    def fingerprint(self, key: bytes) -> str | None:
        """Return a one-way circuit-breaker identifier without exposing a token."""
        with self._lock:
            credentials = self._load()
            if credentials is None:
                return None
            return hmac.new(
                key,
                b"brain-meta-auth-circuit-v1\x00"
                + credentials.access_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

    def _load(self) -> OAuthCredentials | None:
        marker = self._current_store_marker()
        if self._credentials is not None and marker == self._store_marker:
            return self._credentials
        try:
            self._credentials = self._oauth.load_credentials()
        except OAuthError:
            self._state = "degraded"
            return None
        if self._credentials is None:
            self._state = "missing"
        elif self._state == "degraded":
            # A valid envelope may have been atomically replaced by an
            # operator in another process after a transient read failure.
            self._state = "missing"
        elif (
            self._invalidated_access_token is not None
            and self._credentials.access_token != self._invalidated_access_token
        ):
            # A browser login in another process atomically replaced the
            # envelope. Prefer that fresh grant over a refresh of a rejected
            # credential.
            self._force_refresh = False
            self._invalidated_access_token = None
        self._store_marker = self._current_store_marker()
        return self._credentials

    def _current_store_marker(self) -> tuple[int, int, int] | None:
        try:
            result = self._oauth._store_path.stat()
        except OSError:
            return None
        return (result.st_ino, result.st_mtime_ns, result.st_size)


class MetaAdsOAuth:
    """OAuth authorization, token refresh, and encrypted credential storage."""

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None = None,
        store_path: Path = DEFAULT_STORE_PATH,
        key_path: Path = DEFAULT_KEY_PATH,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        metadata_fetcher: MetadataFetcher | None = None,
        token_requester: TokenRequester | None = None,
        registration_requester: RegistrationRequester | None = None,
        dynamic_client: OAuthDynamicClient | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        if (
            (client_id is None and client_secret is not None)
            or (client_id is not None and not _safe_secret_text(client_id))
            or (client_secret is not None and not _safe_secret_text(client_secret))
        ):
            raise ValueError("OAuth client credentials are invalid")
        if dynamic_client is not None and (
            client_id is not None and client_id != dynamic_client.client_id
        ):
            raise ValueError("OAuth client credentials are invalid")
        if redirect_uri != DEFAULT_REDIRECT_URI:
            raise ValueError("OAuth redirect URI is fixed")
        self._client_id = client_id
        self._client_secret = client_secret
        self._store_path = Path(store_path)
        self._key_path = Path(key_path)
        self._redirect_uri = redirect_uri
        self._metadata_fetcher = metadata_fetcher or _fetch_json
        self._token_requester = token_requester or _post_form
        self._registration_requester = registration_requester or (
            lambda url, payload: _post_json(
                url, payload, allowed_hosts={"mcp.facebook.com"}
            )
        )
        self._now = now
        # Static client credentials remain supported for the explicit token
        # fallback.  Keep the compatibility registration in memory only; new
        # envelopes are always written as a DCR-shaped v2 payload.
        self._static_client_fallback = dynamic_client is None and client_id is not None
        if dynamic_client is None and client_id is not None:
            dynamic_client = OAuthDynamicClient(
                client_id=client_id,
                client_secret=client_secret,
                registration_access_token=None,
                registered_at=max(float(now()), 1.0),
                expires_at=None,
                issuer=META_ADS_MCP_RESOURCE,
                resource=META_ADS_MCP_RESOURCE,
                redirect_uri=redirect_uri,
                scopes=frozenset({"ads_read"}),
            )
        self._dynamic_client = dynamic_client
        self._metadata: OAuthMetadata | None = None

    @classmethod
    def from_store(
        cls,
        *,
        store_path: Path = DEFAULT_STORE_PATH,
        key_path: Path = DEFAULT_KEY_PATH,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        metadata_fetcher: MetadataFetcher | None = None,
        token_requester: TokenRequester | None = None,
        registration_requester: RegistrationRequester | None = None,
        now: Callable[[], float] = time.time,
    ) -> MetaAdsOAuth:
        """Backward-compatible alias for :meth:`from_store_or_new`."""
        return cls.from_store_or_new(
            store_path=store_path,
            key_path=key_path,
            redirect_uri=redirect_uri,
            metadata_fetcher=metadata_fetcher,
            token_requester=token_requester,
            registration_requester=registration_requester,
            now=now,
        )

    @classmethod
    def from_store_or_new(
        cls,
        *,
        store_path: Path = DEFAULT_STORE_PATH,
        key_path: Path = DEFAULT_KEY_PATH,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        metadata_fetcher: MetadataFetcher | None = None,
        token_requester: TokenRequester | None = None,
        registration_requester: RegistrationRequester | None = None,
        now: Callable[[], float] = time.time,
    ) -> MetaAdsOAuth:
        """Open a DCR session, or an empty client when no store exists.

        The old pre-registered envelope is intentionally not migrated.  This
        makes an operator explicitly clear the old App ID/App Secret before a
        new dynamic registration is created.
        """
        store = Path(store_path)
        key = Path(key_path)
        oauth = cls(
            client_id=None,
            store_path=store,
            key_path=key,
            redirect_uri=redirect_uri,
            metadata_fetcher=metadata_fetcher,
            token_requester=token_requester,
            registration_requester=registration_requester,
            now=now,
        )
        if not store.exists():
            return oauth
        try:
            registration, _credentials = oauth._load_store()
            oauth._dynamic_client = registration
            oauth._client_id = registration.client_id
            return oauth
        except OAuthError:
            raise
        except (InvalidTag, OSError, TypeError, ValueError, json.JSONDecodeError):
            raise OAuthError("oauth_credentials_invalid") from None

    def save_client_configuration(self) -> None:
        """Retain the legacy writer solely so old stores can be identified.

        Production code must use ``save_registration``.  Deliberately writing
        this shape lets an operator run ``clear`` and migrate explicitly,
        while all DCR readers reject it with ``oauth_legacy_store``.
        """
        configuration = OAuthClientConfiguration(
            client_id=self._client_id,
            client_secret=self._client_secret,
            configured_at=self._now(),
        )
        try:
            self._encrypt_and_save(_client_configuration_mapping(configuration))
        except OAuthError:
            raise
        except (OSError, TypeError, ValueError):
            raise OAuthError("oauth_credentials_unavailable") from None

    def save_registration(self, registration: OAuthDynamicClient) -> None:
        """Persist a validated dynamic registration with no bearer tokens."""
        _validate_dynamic_client(registration)
        try:
            credentials = None
            if self._store_path.exists():
                _old_registration, credentials = self._load_store()
                if _old_registration != registration:
                    credentials = None
            self._dynamic_client = registration
            self._client_id = registration.client_id
            self._encrypt_and_save(_store_mapping(registration, credentials))
        except OAuthError:
            raise
        except (OSError, TypeError, ValueError):
            raise OAuthError("oauth_credentials_unavailable") from None

    def discover(self) -> OAuthMetadata:
        try:
            payload = self._metadata_fetcher(OAUTH_METADATA_URL)
            metadata = _parse_metadata(payload)
        except OAuthError:
            raise
        except Exception:  # noqa: BLE001 - adapters may raise arbitrary transport errors
            raise OAuthError("oauth_metadata_unavailable") from None
        self._metadata = metadata
        return metadata

    def authorization_url(self) -> OAuthAuthorizationRequest:
        metadata = self._metadata or self.discover()
        client_id = self._client_id
        if client_id is None:
            client_id = self.ensure_dynamic_client().client_id
        # A published optional scope is not evidence that it is required.
        # Keep the consent request to the minimum read-only permission; an
        # operator can make any future elevation explicit and auditable.
        scopes = {"ads_read"}
        verifier = _token_urlsafe(64)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _token_urlsafe(32)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": " ".join(sorted(scopes)),
                "resource": META_ADS_MCP_RESOURCE,
            }
        )
        return OAuthAuthorizationRequest(
            url=f"{metadata.authorization_endpoint}?{query}",
            state=state,
            code_verifier=verifier,
            requested_scopes=frozenset(scopes),
            expected_issuer=metadata.issuer,
            require_issuer=metadata.authorization_response_iss_parameter_supported,
        )

    def ensure_dynamic_client(self) -> OAuthDynamicClient:
        """Return a valid registration, creating one when necessary."""
        metadata = self._metadata or self.discover()
        client = self._dynamic_client
        if client is not None and (
            client.issuer != metadata.issuer
            or client.resource != metadata.resource
            or client.redirect_uri != self._redirect_uri
            or client.scopes != frozenset({"ads_read"})
            or (client.expires_at is not None and client.expires_at <= self._now())
        ):
            client = None
        if client is None:
            client = self.register_dynamic_client()
            self._dynamic_client = client
        self._client_id = client.client_id
        return client

    def register_dynamic_client(self) -> OAuthDynamicClient:
        """Register the fixed public Brain client with the discovered server."""
        metadata = self._metadata or self.discover()
        payload: dict[str, object] = {
            "redirect_uris": [self._redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": _DYNAMIC_CLIENT_NAME,
            "scope": "ads_read",
        }
        try:
            response = self._registration_requester(
                metadata.registration_endpoint, payload
            )
            if not isinstance(response, Mapping):
                raise TypeError
            client = _dynamic_client_from_registration(
                response,
                metadata,
                redirect_uri=self._redirect_uri,
                now=self._now(),
            )
        except OAuthError:
            raise
        except (TypeError, ValueError):
            raise OAuthError("oauth_registration_invalid") from None
        except Exception:  # noqa: BLE001 - adapters may raise arbitrary transport errors
            raise OAuthError("oauth_registration_unavailable") from None
        self._dynamic_client = client
        return client

    def exchange_code(
        self, code: str, request: OAuthAuthorizationRequest
    ) -> OAuthCredentials:
        if not _safe_secret_text(code):
            raise OAuthError("oauth_token_invalid")
        metadata = self._metadata or self.discover()
        form = self._base_form("authorization_code")
        form.update(
            {
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": request.code_verifier,
            }
        )
        payload = self._request_token(metadata.token_endpoint, form)
        return self._credentials_from_token_response(
            payload, metadata, request.requested_scopes, previous=None
        )

    def refresh(self, credentials: OAuthCredentials) -> OAuthCredentials:
        _validate_credentials(credentials)
        metadata = self._metadata or self.discover()
        registration = self._dynamic_client or self.load_registration()
        if registration is None:
            raise OAuthError("oauth_credentials_invalid")
        self._dynamic_client = registration
        self._client_id = registration.client_id
        if (
            credentials.client_id != registration.client_id
            or credentials.issuer != metadata.issuer
            or credentials.resource != META_ADS_MCP_RESOURCE
        ):
            raise OAuthError("oauth_credentials_invalid")
        if (
            registration.expires_at is not None
            and registration.expires_at <= self._now()
        ):
            raise OAuthError("oauth_credentials_unavailable")
        # DCR registered this client with token_endpoint_auth_method=none.
        # Any returned client_secret is retained encrypted for diagnostics or
        # future protocol changes, but is never sent as token authentication.
        form = self._base_form("refresh_token")
        form["refresh_token"] = credentials.refresh_token
        payload = self._request_token(metadata.token_endpoint, form)
        if payload.get("error") == "invalid_grant":
            self.invalidate_credentials()
            raise OAuthError("oauth_invalid_grant")
        return self._credentials_from_token_response(
            payload, metadata, credentials.scopes, previous=credentials
        )

    def load_credentials(self) -> OAuthCredentials | None:
        try:
            if not self._store_path.exists():
                return None
            registration, credentials = self._load_store()
            self._dynamic_client = registration
            self._client_id = registration.client_id
            if credentials is None:
                return None
            if credentials.client_id != registration.client_id:
                raise ValueError("OAuth client ID does not match stored credentials")
            return credentials
        except OAuthError:
            raise
        except (InvalidTag, OSError, TypeError, ValueError, json.JSONDecodeError):
            raise OAuthError("oauth_credentials_invalid") from None

    def load_registration(self) -> OAuthDynamicClient | None:
        try:
            if not self._store_path.exists():
                return None
            registration, _credentials = self._load_store()
            self._dynamic_client = registration
            self._client_id = registration.client_id
            return registration
        except OAuthError:
            raise
        except (InvalidTag, OSError, TypeError, ValueError, json.JSONDecodeError):
            raise OAuthError("oauth_credentials_invalid") from None

    def registration_state(self, now: float) -> RegistrationState:
        """Report registration validity without initiating DCR."""
        try:
            registration = self._dynamic_client or self.load_registration()
        except OAuthError:
            return "degraded"
        if registration is None:
            return "missing"
        if not _finite_number(now):
            return "degraded"
        if registration.expires_at is not None and registration.expires_at <= now:
            return "expired"
        return "ready"

    def save_credentials(self, credentials: OAuthCredentials) -> None:
        _validate_credentials(credentials)
        registration = self._dynamic_client or self.load_registration()
        if registration is None or credentials.client_id != registration.client_id:
            raise OAuthError("oauth_credentials_invalid")
        try:
            self._dynamic_client = registration
            self._client_id = registration.client_id
            self._encrypt_and_save(_store_mapping(registration, credentials))
        except OAuthError:
            raise
        except (OSError, TypeError, ValueError):
            raise OAuthError("oauth_credentials_unavailable") from None

    def _load_store(self) -> tuple[OAuthDynamicClient, OAuthCredentials | None]:
        payload = self._decrypt_payload()
        if payload.get("kind") == "client_configuration":
            raise OAuthError("oauth_legacy_store")
        if set(payload) != _STORE_PAYLOAD_FIELDS:
            raise ValueError("OAuth store payload is invalid")
        if payload.get("version") != 2:
            raise ValueError("OAuth store payload version is invalid")
        registration = _dynamic_client_from_mapping(payload.get("dynamic_client"))
        credentials_payload = payload.get("credentials")
        credentials = None
        if credentials_payload is not None:
            if not isinstance(credentials_payload, Mapping):
                raise ValueError("OAuth credentials payload is invalid")
            credentials = _credentials_from_mapping(credentials_payload)
            if credentials.client_id != registration.client_id:
                raise ValueError("OAuth client ID does not match registration")
        return registration, credentials

    def _decrypt_payload(self) -> dict[str, object]:
        ciphertext = self._read_private_file(self._store_path, _MAX_ENVELOPE_BYTES)
        outer = _strict_json_object(ciphertext)
        outer_version = outer.get("version")
        if (
            set(outer) != _ENVELOPE_FIELDS
            or not isinstance(outer_version, int)
            or isinstance(outer_version, bool)
            or outer_version not in {1, 2}
        ):
            raise TypeError
        nonce = _decode_base64(outer["nonce"])
        encrypted = _decode_base64(outer["ciphertext"])
        if len(nonce) != 12 or not encrypted:
            raise ValueError
        plaintext = AESGCM(self._read_key()).decrypt(nonce, encrypted, _AAD)
        payload = _strict_json_object(plaintext)
        if outer_version == 1:
            # Version 1 is decrypted only far enough to identify the old
            # pre-registered shape; no other v1 content may be interpreted.
            if payload.get("kind") != "client_configuration":
                raise ValueError("OAuth legacy envelope payload is invalid")
            return payload
        if (
            not isinstance(payload.get("version"), int)
            or isinstance(payload.get("version"), bool)
            or payload.get("version") != 2
        ):
            if payload.get("kind") == "client_configuration":
                return payload
            raise ValueError("OAuth envelope payload version is invalid")
        return payload

    def _encrypt_and_save(self, payload: Mapping[str, object]) -> None:
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        nonce = os.urandom(12)
        encrypted = AESGCM(self._read_key()).encrypt(nonce, plaintext, _AAD)
        envelope = json.dumps(
            {
                "version": 2,
                "nonce": _base64url(nonce),
                "ciphertext": _base64url(encrypted),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_private_write(self._store_path, envelope)

    def clear_credentials(self) -> None:
        self.clear_store(self._store_path)

    def invalidate_credentials(self) -> None:
        """Atomically remove tokens while retaining dynamic registration."""
        try:
            registration = self._dynamic_client or self.load_registration()
            if registration is None:
                self.clear_store(self._store_path)
                return
            self._dynamic_client = registration
            self._client_id = registration.client_id
            self._encrypt_and_save(_store_mapping(registration, None))
        except OAuthError:
            # If the existing envelope cannot be rewritten, make sure the
            # revoked token is not left available for another process.
            self.clear_store(self._store_path)
            raise

    @staticmethod
    def clear_store(store_path: Path) -> None:
        """Remove one private envelope without decrypting or trusting it."""
        try:
            store_path = Path(store_path)
            directory = MetaAdsOAuth._open_private_directory(store_path.parent)
            try:
                try:
                    file_stat = os.stat(
                        store_path.name, dir_fd=directory, follow_symlinks=False
                    )
                except FileNotFoundError:
                    return
                MetaAdsOAuth._validate_private_file_stat(file_stat, _MAX_ENVELOPE_BYTES)
                os.unlink(store_path.name, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        except OAuthError:
            raise
        except ValueError:
            raise OAuthError("oauth_credentials_invalid") from None
        except OSError:
            raise OAuthError("oauth_credentials_unavailable") from None

    def _base_form(
        self, grant_type: str, *, client_secret: str | None = None
    ) -> dict[str, str]:
        if self._client_id is None:
            raise OAuthError("oauth_credentials_invalid")
        form = {
            "grant_type": grant_type,
            "client_id": self._client_id,
            "resource": META_ADS_MCP_RESOURCE,
        }
        secret = self._client_secret if self._static_client_fallback else client_secret
        if secret is not None:
            form["client_secret"] = secret
        return form

    def _request_token(
        self, endpoint: str, form: dict[str, str]
    ) -> Mapping[str, object]:
        try:
            response = self._token_requester(endpoint, form)
            if not isinstance(response, Mapping):
                raise TypeError
            return cast(Mapping[str, object], response)
        except OAuthError:
            raise
        except (TypeError, ValueError):
            raise OAuthError("oauth_token_invalid") from None
        except Exception:  # noqa: BLE001 - adapters may raise arbitrary transport errors
            raise OAuthError("oauth_token_unavailable") from None

    def _credentials_from_token_response(
        self,
        payload: Mapping[str, object],
        metadata: OAuthMetadata,
        expected_scopes: frozenset[str],
        *,
        previous: OAuthCredentials | None,
    ) -> OAuthCredentials:
        try:
            registration = self._dynamic_client or self.load_registration()
            if registration is None:
                raise ValueError
            self._dynamic_client = registration
            self._client_id = registration.client_id
            access_token = _required_secret(payload, "access_token")
            expires_in = _positive_seconds(payload.get("expires_in"))
            refresh_token = _optional_secret(payload.get("refresh_token"))
            if refresh_token is None:
                if previous is None:
                    raise ValueError
                refresh_token = previous.refresh_token
            scopes = _response_scopes(payload.get("scope"), expected_scopes)
            current = self._now()
            if not _finite_number(current):
                raise ValueError
            refresh_lifetime = payload.get("refresh_token_expires_in")
            refresh_expires_at = (
                current + _positive_seconds(refresh_lifetime)
                if refresh_lifetime is not None
                else (previous.refresh_expires_at if previous is not None else None)
            )
            return OAuthCredentials(
                client_id=self._client_id,
                client_secret=registration.client_secret,
                access_token=access_token,
                refresh_token=refresh_token,
                access_expires_at=current + expires_in,
                refresh_expires_at=refresh_expires_at,
                scopes=scopes,
                issuer=metadata.issuer,
                resource=metadata.resource,
                created_at=current if previous is None else previous.created_at,
                updated_at=current,
            )
        except OAuthError:
            raise
        except (TypeError, ValueError):
            raise OAuthError("oauth_token_invalid") from None

    def _read_key(self) -> bytes:
        key = self._read_private_file(self._key_path, _MAX_KEY_BYTES)
        if len(key) != 32:
            raise ValueError("OAuth key is invalid")
        return key

    @staticmethod
    def _read_private_file(path: Path, maximum_bytes: int) -> bytes:
        directory = MetaAdsOAuth._open_private_directory(path.parent)
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path.name, flags, dir_fd=directory)
            initial_stat = os.fstat(descriptor)
            MetaAdsOAuth._validate_private_file_stat(initial_stat, maximum_bytes)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(8192, maximum_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    raise ValueError("OAuth credential file is too large")
            final_stat = os.fstat(descriptor)
            MetaAdsOAuth._validate_private_file_stat(final_stat, maximum_bytes)
            if final_stat.st_size != total:
                raise ValueError("OAuth credential file changed while reading")
            return b"".join(chunks)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    @staticmethod
    def _atomic_private_write(path: Path, content: bytes) -> None:
        if len(content) > _MAX_ENVELOPE_BYTES:
            raise ValueError("OAuth envelope is too large")
        directory = MetaAdsOAuth._open_private_directory(path.parent, create=True)
        temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            os.fsync(directory)
        except Exception:
            try:
                os.unlink(temporary_name, dir_fd=directory)
            except OSError:
                pass
            raise
        finally:
            os.close(directory)

    @staticmethod
    def _open_private_directory(path: Path, *, create: bool = False) -> int:
        if create:
            try:
                path.lstat()
            except FileNotFoundError:
                path.mkdir(mode=0o700, parents=True, exist_ok=False)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        try:
            directory_stat = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != 0
                or stat.S_IMODE(directory_stat.st_mode) & 0o077
            ):
                raise ValueError("OAuth credential directory is not private")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _validate_private_file_stat(
        file_stat: os.stat_result, maximum_bytes: int
    ) -> None:
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != 0
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_size < 0
            or file_stat.st_size > maximum_bytes
        ):
            raise ValueError("OAuth credential file permissions are invalid")


def _parse_metadata(payload: object) -> OAuthMetadata:
    if not isinstance(payload, Mapping):
        raise OAuthError("oauth_metadata_invalid")
    issuer = payload.get("issuer")
    resource = payload.get("resource", META_ADS_MCP_RESOURCE)
    authorization_endpoint = payload.get("authorization_endpoint")
    token_endpoint = payload.get("token_endpoint")
    registration_endpoint = payload.get("registration_endpoint")
    scopes = payload.get("scopes_supported", ())
    issuer_parameter_supported = payload.get(
        "authorization_response_iss_parameter_supported", False
    )
    methods = payload.get("code_challenge_methods_supported", ())
    grants = payload.get("grant_types_supported", ())
    responses = payload.get("response_types_supported", ())
    if (
        issuer != META_ADS_MCP_RESOURCE
        or resource != META_ADS_MCP_RESOURCE
        or not _allowed_https_url(issuer, {"mcp.facebook.com"})
        or not _allowed_https_url(authorization_endpoint, {"www.facebook.com"})
        or not _allowed_https_url(token_endpoint, {"graph.facebook.com"})
        or not _allowed_https_url(registration_endpoint, {"mcp.facebook.com"})
        or not _string_set(scopes).issuperset({"ads_read"})
        or "S256" not in _string_set(methods)
        or not _string_set(grants).issuperset({"authorization_code", "refresh_token"})
        or "code" not in _string_set(responses)
        or not isinstance(issuer_parameter_supported, bool)
    ):
        raise OAuthError("oauth_metadata_invalid")
    return OAuthMetadata(
        resource=META_ADS_MCP_RESOURCE,
        issuer=issuer,
        authorization_endpoint=cast(str, authorization_endpoint),
        token_endpoint=cast(str, token_endpoint),
        registration_endpoint=cast(str, registration_endpoint),
        scopes_supported=frozenset(_string_set(scopes) & ALLOWED_SCOPES),
        authorization_response_iss_parameter_supported=issuer_parameter_supported,
    )


def _validate_credentials(credentials: OAuthCredentials) -> None:
    if (
        credentials.version != 1
        or not _safe_secret_text(credentials.client_id)
        or (
            credentials.client_secret is not None
            and not _safe_secret_text(credentials.client_secret)
        )
        or not _safe_secret_text(credentials.access_token)
        or not _safe_secret_text(credentials.refresh_token)
        or not _finite_number(credentials.access_expires_at)
        or (
            credentials.refresh_expires_at is not None
            and not _finite_number(credentials.refresh_expires_at)
        )
        or not credentials.scopes
        or not credentials.scopes.issubset(ALLOWED_SCOPES)
        or "ads_read" not in credentials.scopes
        or credentials.issuer != META_ADS_MCP_RESOURCE
        or credentials.resource != META_ADS_MCP_RESOURCE
        or not _finite_number(credentials.created_at)
        or not _finite_number(credentials.updated_at)
        or credentials.updated_at < credentials.created_at
        or credentials.access_expires_at <= 0
        or (
            credentials.refresh_expires_at is not None
            and credentials.refresh_expires_at <= 0
        )
    ):
        raise ValueError("OAuth credentials are invalid")


def _credentials_mapping(credentials: OAuthCredentials) -> dict[str, object]:
    return {
        "version": credentials.version,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "access_expires_at": credentials.access_expires_at,
        "refresh_expires_at": credentials.refresh_expires_at,
        "scopes": sorted(credentials.scopes),
        "issuer": credentials.issuer,
        "resource": credentials.resource,
        "created_at": credentials.created_at,
        "updated_at": credentials.updated_at,
    }


def _dynamic_client_mapping(client: OAuthDynamicClient) -> dict[str, object]:
    _validate_dynamic_client(client)
    return {
        "version": 1,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "registration_access_token": client.registration_access_token,
        "registered_at": client.registered_at,
        "expires_at": client.expires_at,
        "issuer": client.issuer,
        "resource": client.resource,
        "redirect_uri": client.redirect_uri,
        "scopes": sorted(client.scopes),
    }


def _store_mapping(
    registration: OAuthDynamicClient, credentials: OAuthCredentials | None
) -> dict[str, object]:
    _validate_dynamic_client(registration)
    if credentials is not None:
        _validate_credentials(credentials)
        if credentials.client_id != registration.client_id:
            raise ValueError("OAuth client ID does not match registration")
    return {
        "version": 2,
        "dynamic_client": _dynamic_client_mapping(registration),
        "credentials": (
            None if credentials is None else _credentials_mapping(credentials)
        ),
    }


def _client_configuration_mapping(
    configuration: OAuthClientConfiguration,
) -> dict[str, object]:
    return {
        "version": 1,
        "kind": "client_configuration",
        "client_id": configuration.client_id,
        "client_secret": configuration.client_secret,
        "configured_at": configuration.configured_at,
    }


def _client_configuration_from_mapping(
    payload: Mapping[str, object],
) -> OAuthClientConfiguration:
    if (
        set(payload) != _CLIENT_CONFIGURATION_FIELDS
        or not isinstance(payload.get("version"), int)
        or isinstance(payload.get("version"), bool)
        or payload.get("version") != 1
        or payload.get("kind") != "client_configuration"
    ):
        raise ValueError("OAuth client configuration fields are invalid")
    return OAuthClientConfiguration(
        client_id=payload.get("client_id"),  # type: ignore[arg-type]
        client_secret=payload.get("client_secret"),  # type: ignore[arg-type]
        configured_at=payload.get("configured_at"),  # type: ignore[arg-type]
    )


def _dynamic_client_from_mapping(payload: object) -> OAuthDynamicClient:
    if not isinstance(payload, Mapping) or set(payload) != _DYNAMIC_CLIENT_FIELDS:
        raise ValueError("OAuth dynamic client fields are invalid")
    if (
        not isinstance(payload.get("version"), int)
        or isinstance(payload.get("version"), bool)
        or payload.get("version") != 1
    ):
        raise ValueError("OAuth dynamic client version is invalid")
    scopes_raw = payload.get("scopes")
    if not isinstance(scopes_raw, list) or not all(
        isinstance(item, str) for item in scopes_raw
    ):
        raise ValueError("OAuth dynamic client scopes are invalid")
    return OAuthDynamicClient(
        client_id=payload.get("client_id"),  # type: ignore[arg-type]
        client_secret=payload.get("client_secret"),  # type: ignore[arg-type]
        registration_access_token=payload.get("registration_access_token"),  # type: ignore[arg-type]
        registered_at=payload.get("registered_at"),  # type: ignore[arg-type]
        expires_at=payload.get("expires_at"),  # type: ignore[arg-type]
        issuer=payload.get("issuer"),  # type: ignore[arg-type]
        resource=payload.get("resource"),  # type: ignore[arg-type]
        redirect_uri=payload.get("redirect_uri"),  # type: ignore[arg-type]
        scopes=frozenset(scopes_raw),
    )


def _validate_dynamic_client(client: OAuthDynamicClient) -> None:
    if not isinstance(client, OAuthDynamicClient):
        raise TypeError("OAuth dynamic client is invalid")
    # Re-run dataclass validation to protect against unsafe object construction
    # and future mutable fields.
    OAuthDynamicClient(
        client_id=client.client_id,
        client_secret=client.client_secret,
        registration_access_token=client.registration_access_token,
        registered_at=client.registered_at,
        expires_at=client.expires_at,
        issuer=client.issuer,
        resource=client.resource,
        redirect_uri=client.redirect_uri,
        scopes=client.scopes,
    )


def _credentials_from_mapping(payload: Mapping[str, object]) -> OAuthCredentials:
    if set(payload) != _CREDENTIAL_FIELDS:
        raise ValueError("OAuth credential fields are invalid")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("OAuth credential version is invalid")
    scopes_raw = payload.get("scopes")
    if not isinstance(scopes_raw, list) or not all(
        isinstance(item, str) for item in scopes_raw
    ):
        raise ValueError("OAuth credential scopes are invalid")
    return OAuthCredentials(
        client_id=payload.get("client_id"),  # type: ignore[arg-type]
        client_secret=payload.get("client_secret"),  # type: ignore[arg-type]
        access_token=payload.get("access_token"),  # type: ignore[arg-type]
        refresh_token=payload.get("refresh_token"),  # type: ignore[arg-type]
        access_expires_at=payload.get("access_expires_at"),  # type: ignore[arg-type]
        refresh_expires_at=payload.get("refresh_expires_at"),  # type: ignore[arg-type]
        scopes=frozenset(scopes_raw),
        issuer=payload.get("issuer"),  # type: ignore[arg-type]
        resource=payload.get("resource"),  # type: ignore[arg-type]
        created_at=payload.get("created_at"),  # type: ignore[arg-type]
        updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
    )


def _read_http_json(response: object) -> object:
    headers = getattr(response, "headers", None)
    content_length = None if headers is None else headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            raise ValueError("OAuth response size is invalid") from None
        if declared < 0 or declared > _MAX_HTTP_JSON_BYTES:
            raise ValueError("OAuth response is too large")
    body = response.read(_MAX_HTTP_JSON_BYTES + 1)  # type: ignore[attr-defined]
    if len(body) > _MAX_HTTP_JSON_BYTES:
        raise ValueError("OAuth response is too large")
    return json.loads(body.decode("utf-8"))


def _fetch_json(url: str) -> object:
    request = Request(url, headers={"Accept": "application/json"})
    with _NO_REDIRECT_OPENER.open(request, timeout=10) as response:
        return _read_http_json(response)


def _post_form(url: str, form: dict[str, str]) -> object:
    request = Request(
        url,
        data=urlencode(form).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=10) as response:
        return _read_http_json(response)


def _post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    allowed_hosts: set[str] | None = None,
) -> object:
    """POST a bounded JSON object without following redirects.

    Endpoint allowlisting is supplied by the caller because the generic helper
    is also useful with local HTTP fixtures. Production DCR calls always pass
    the metadata-derived Meta allowlist before reaching this helper.
    """
    if allowed_hosts is not None and not _allowed_https_url(url, allowed_hosts):
        raise ValueError("OAuth endpoint is not allowlisted")
    try:
        body = json.dumps(
            payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("OAuth request is invalid") from None
    if len(body) > _MAX_HTTP_JSON_BYTES:
        raise ValueError("OAuth request is too large")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=10) as response:
        return _read_http_json(response)


def _strict_json_object(value: bytes) -> dict[str, object]:
    parsed = json.loads(value.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError("expected JSON object")
    return cast(dict[str, object], parsed)


def _allowed_https_url(value: object, hosts: set[str]) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _string_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        return set()
    return set(cast(list[str] | tuple[str, ...], value))


def _token_urlsafe(size: int) -> str:
    return secrets.token_urlsafe(size)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("base64 value is invalid")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _safe_secret_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 16_384
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _single_query_value(value: object) -> str | None:
    if isinstance(value, str):
        return value if _safe_secret_text(value) else None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0] if _safe_secret_text(value[0]) else None
    return None


def _required_secret(payload: Mapping[str, object], field_name: str) -> str:
    value = _optional_secret(payload.get(field_name))
    if value is None:
        raise ValueError
    return value


def _optional_secret(value: object) -> str | None:
    if value is None:
        return None
    return value if _safe_secret_text(value) else None


def _dynamic_client_from_registration(
    payload: Mapping[str, object],
    metadata: OAuthMetadata,
    *,
    redirect_uri: str,
    now: float,
) -> OAuthDynamicClient:
    """Validate an RFC 7591 response while retaining only secret fields."""
    if not _finite_number(now) or now <= 0:
        raise ValueError
    client_id = _required_secret(payload, "client_id")
    client_secret = _registration_optional_secret(payload, "client_secret")
    registration_access_token = _registration_optional_secret(
        payload, "registration_access_token"
    )

    redirect_uris = payload.get("redirect_uris")
    if redirect_uris is not None and redirect_uris != [redirect_uri]:
        raise ValueError
    grant_types = payload.get("grant_types")
    if grant_types is not None and grant_types != [
        "authorization_code",
        "refresh_token",
    ]:
        raise ValueError
    response_types = payload.get("response_types")
    if response_types is not None and response_types != ["code"]:
        raise ValueError
    if payload.get("token_endpoint_auth_method", "none") != "none":
        raise ValueError
    if payload.get("scope") is not None and _response_scopes(
        payload.get("scope"), frozenset({"ads_read"})
    ) != frozenset({"ads_read"}):
        raise ValueError

    issued_at = payload.get("client_id_issued_at", now)
    if not _finite_number(issued_at) or float(issued_at) <= 0:
        raise ValueError
    expiry_value = payload.get("client_secret_expires_at")
    if expiry_value is None:
        expiry_value = payload.get("client_expires_at")
    if expiry_value is None or expiry_value == 0:
        expires_at = None
    elif not _finite_number(expiry_value) or float(expiry_value) <= 0:
        raise ValueError
    else:
        expires_at = float(expiry_value)
    return OAuthDynamicClient(
        client_id=client_id,
        client_secret=client_secret,
        registration_access_token=registration_access_token,
        registered_at=float(issued_at),
        expires_at=expires_at,
        issuer=metadata.issuer,
        resource=metadata.resource,
        redirect_uri=redirect_uri,
        scopes=frozenset({"ads_read"}),
    )


def _registration_optional_secret(
    payload: Mapping[str, object], field_name: str
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not _safe_secret_text(value):
        raise ValueError
    return value


def _positive_seconds(value: object) -> float:
    if not _finite_number(value) or float(value) <= 0:
        raise ValueError
    return float(value)


def _response_scopes(value: object, expected: frozenset[str]) -> frozenset[str]:
    if value is None:
        scopes = expected
    elif isinstance(value, str):
        scopes = frozenset(part for part in value.split(" ") if part)
    else:
        raise ValueError
    if (
        not scopes
        or not scopes.issubset(ALLOWED_SCOPES)
        or "ads_read" not in scopes
        or scopes != expected
    ):
        raise ValueError
    return scopes


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
