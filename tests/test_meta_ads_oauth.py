from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import anyio

from brain.meta_ads_oauth import (
    META_ADS_MCP_RESOURCE,
    MetaAdsOAuth,
    OAuthCallback,
    OAuthCredentialProvider,
    OAuthCredentials,
    OAuthError,
    _post_form,
)

AUTH_METADATA = {
    "issuer": META_ADS_MCP_RESOURCE,
    "authorization_endpoint": "https://www.facebook.com/v99.0/dialog/oauth",
    "token_endpoint": "https://graph.facebook.com/v99.0/oauth/access_token",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["ads_read", "ads_mcp_management", "ads_management"],
}


class MetaAdsOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = root / "credentials" / "oauth.enc"
        self.key = root / "oauth.key"
        self.key.write_bytes(os.urandom(32))
        os.chmod(self.key, 0o600)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.oauth = MetaAdsOAuth(
            client_id="app-client-id",
            client_secret="very-secret-app-secret",
            store_path=self.store,
            key_path=self.key,
            metadata_fetcher=self._metadata,
            token_requester=self._token_request,
            now=lambda: 1_700_000_000.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _metadata(self, url: str) -> object:
        self.assertEqual(
            url, "https://mcp.facebook.com/.well-known/oauth-authorization-server/ads"
        )
        return AUTH_METADATA

    def _token_request(self, url: str, form: dict[str, str]) -> object:
        self.calls.append((url, form))
        if form["grant_type"] == "authorization_code":
            return {
                "access_token": "access-token-value",
                "refresh_token": "refresh-token-value",
                "expires_in": 3600,
                "refresh_token_expires_in": 7200,
                "scope": "ads_read",
            }
        return {
            "access_token": "refreshed-access-token",
            "refresh_token": "rotated-refresh-token",
            "expires_in": 3600,
            "scope": "ads_read",
        }

    def test_discover_validates_metadata_and_allowlisted_endpoints(self) -> None:
        metadata = self.oauth.discover()
        self.assertEqual(metadata.resource, META_ADS_MCP_RESOURCE)
        self.assertEqual(metadata.issuer, META_ADS_MCP_RESOURCE)
        self.assertIn("ads_mcp_management", metadata.scopes_supported)

        for key, value in (
            ("issuer", "http://mcp.facebook.com/ads"),
            ("issuer", "https://evil.example/ads"),
            ("authorization_endpoint", "http://www.facebook.com/dialog/oauth"),
            ("authorization_endpoint", "https://evil.example/dialog/oauth"),
            ("token_endpoint", "https://evil.example/token"),
        ):
            payload = dict(AUTH_METADATA)
            payload[key] = value
            oauth = MetaAdsOAuth(
                client_id="client",
                store_path=self.store,
                key_path=self.key,
                metadata_fetcher=lambda _url, payload=payload: payload,
            )
            with self.assertRaisesRegex(OAuthError, "^oauth_metadata_invalid$"):
                oauth.discover()

    def test_authorization_url_uses_pkce_and_read_only_scopes(self) -> None:
        request = self.oauth.authorization_url()
        query = parse_qs(urlparse(request.url).query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["app-client-id"])
        self.assertEqual(
            query["redirect_uri"], ["http://127.0.0.1:8766/oauth/callback"]
        )
        self.assertEqual(query["state"], [request.state])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])
        self.assertEqual(set(query["scope"][0].split()), {"ads_read"})
        self.assertEqual(query["resource"], [META_ADS_MCP_RESOURCE])
        self.assertNotIn("ads_management", query["scope"][0])
        self.assertNotIn("very-secret-app-secret", repr(request))

    def test_callback_is_one_shot_and_validates_state_and_code(self) -> None:
        request = self.oauth.authorization_url()
        callback = OAuthCallback(request)
        self.assertEqual(
            callback.consume({"state": request.state, "code": "code-value"}),
            "code-value",
        )
        for query in (
            {"state": request.state, "code": "again"},
            {"state": "wrong", "code": "code"},
            {"state": request.state},
            {"code": "code"},
        ):
            with self.assertRaisesRegex(OAuthError, "^oauth_callback_invalid$"):
                callback.consume(query)

    def test_exchange_and_refresh_are_sanitized_and_rotate_tokens(self) -> None:
        request = self.oauth.authorization_url()
        credentials = self.oauth.exchange_code("authorization-code", request)
        self.assertEqual(credentials.access_token, "access-token-value")
        self.assertEqual(credentials.refresh_token, "refresh-token-value")
        self.assertEqual(self.calls[0][1]["code_verifier"], request.code_verifier)
        self.assertEqual(self.calls[0][1]["client_secret"], "very-secret-app-secret")
        self.assertEqual(self.calls[0][1]["resource"], META_ADS_MCP_RESOURCE)
        refreshed = self.oauth.refresh(credentials)
        self.assertEqual(refreshed.access_token, "refreshed-access-token")
        self.assertEqual(refreshed.refresh_token, "rotated-refresh-token")
        self.assertEqual(refreshed.refresh_expires_at, credentials.refresh_expires_at)
        self.assertEqual(self.calls[1][1]["resource"], META_ADS_MCP_RESOURCE)
        self.assertNotIn("access-token-value", repr(credentials))
        self.assertNotIn("refresh-token-value", repr(credentials))
        self.assertNotIn("very-secret-app-secret", repr(credentials))

    def test_invalid_grant_persists_no_tokens_and_allows_reauthorization(self) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret="very-secret-app-secret",
            access_token="expired-access-token",
            refresh_token="revoked-refresh-token",
            access_expires_at=1_700_000_100.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.oauth.save_credentials(credentials)

        def invalid_grant(_url: str, _form: dict[str, str]) -> object:
            return {"error": "invalid_grant"}

        oauth = MetaAdsOAuth(
            client_id="app-client-id",
            client_secret="very-secret-app-secret",
            store_path=self.store,
            key_path=self.key,
            metadata_fetcher=self._metadata,
            token_requester=invalid_grant,
            now=lambda: 1_700_000_000.0,
        )
        provider = OAuthCredentialProvider(oauth)
        with self.assertRaisesRegex(OAuthError, "^oauth_invalid_grant$"):
            provider.access_token(1_700_000_000.0)

        self.assertIsNone(oauth.load_credentials())
        self.assertEqual(
            OAuthCredentialProvider(oauth).status(1_700_000_000.0), "missing"
        )
        # App registration remains available, so a subsequent login need not
        # ask for the client secret again.
        self.assertEqual(
            MetaAdsOAuth.from_store(
                store_path=self.store, key_path=self.key
            )._client_id,
            "app-client-id",
        )

    def test_provider_refreshes_near_expiry_once_and_persists_rotated_tokens(
        self,
    ) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="nearly-expired-token",
            refresh_token="refresh-token-value",
            access_expires_at=1_700_000_500.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.oauth.save_credentials(credentials)
        provider = OAuthCredentialProvider(self.oauth)

        self.assertEqual(
            provider.access_token(1_700_000_000.0), "refreshed-access-token"
        )
        self.assertEqual(
            provider.access_token(1_700_000_001.0), "refreshed-access-token"
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.oauth.load_credentials().refresh_token, "rotated-refresh-token"
        )

    def test_provider_fingerprint_uses_the_shared_null_separator(self) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="access-token-value",
            refresh_token="refresh-token-value",
            access_expires_at=1_700_003_600.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.oauth.save_credentials(credentials)
        provider = OAuthCredentialProvider(self.oauth)
        key = b"k" * 32
        expected = hmac.new(
            key,
            b"brain-meta-auth-circuit-v1\x00" + b"access-token-value",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(provider.fingerprint(key), expected)

    def test_provider_reloads_an_externally_reauthorized_store_on_invalidate(
        self,
    ) -> None:
        old = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            access_expires_at=1_700_003_600.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        replacement = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            access_expires_at=1_700_003_700.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_100.0,
        )
        self.oauth.save_credentials(old)
        provider = OAuthCredentialProvider(self.oauth)
        self.assertEqual(provider.access_token(1_700_000_000.0), "old-access-token")
        self.oauth.save_credentials(replacement)
        provider.invalidate()

        self.assertEqual(provider.access_token(1_700_000_100.0), "new-access-token")
        self.assertEqual(
            provider.fingerprint(b"k" * 32),
            hmac.new(
                b"k" * 32,
                b"brain-meta-auth-circuit-v1\x00new-access-token",
                hashlib.sha256,
            ).hexdigest(),
        )

    def test_provider_async_refresh_abandons_a_blocked_thread_at_the_budget(
        self,
    ) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="nearly-expired-token",
            refresh_token="refresh-token-value",
            access_expires_at=1_700_000_500.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.oauth.save_credentials(credentials)
        provider = OAuthCredentialProvider(self.oauth)
        blocked = threading.Event()

        def refresh(_: OAuthCredentials) -> OAuthCredentials:
            blocked.wait(1)
            return credentials

        with patch.object(self.oauth, "refresh", side_effect=refresh):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                anyio.run(provider.access_token_async, 1_700_000_000.0, 0.02)
            self.assertLess(time.monotonic() - started, 0.25)
        blocked.set()

    def test_encrypted_store_round_trip_rejects_wrong_key_and_tampering(self) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret="very-secret-app-secret",
            access_token="access-token-value",
            refresh_token="refresh-token-value",
            access_expires_at=1_700_003_600.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.oauth.save_credentials(credentials)
        self.assertEqual(self.oauth.load_credentials(), credentials)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertNotIn("access-token-value", self.store.read_text())
        wrong_key = self.store.parent / "wrong.key"
        wrong_key.write_bytes(os.urandom(32))
        os.chmod(wrong_key, 0o600)
        wrong = MetaAdsOAuth(
            client_id="app-client-id", store_path=self.store, key_path=wrong_key
        )
        with self.assertRaisesRegex(OAuthError, "^oauth_credentials_invalid$"):
            wrong.load_credentials()
        raw = bytearray(self.store.read_bytes())
        raw[-1] ^= 1
        self.store.write_bytes(raw)
        with self.assertRaisesRegex(OAuthError, "^oauth_credentials_invalid$"):
            self.oauth.load_credentials()

    def test_clear_only_removes_the_expected_store(self) -> None:
        sibling = self.store.parent / "do-not-remove"
        sibling.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(sibling.parent, 0o700)
        sibling.write_text("keep")
        self.store.write_text("placeholder")
        os.chmod(self.store, 0o600)
        self.oauth.clear_credentials()
        self.assertFalse(self.store.exists())
        self.assertTrue(sibling.exists())

    def test_clear_store_removes_corrupt_envelope_without_decrypting(self) -> None:
        self.store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store.write_bytes(b"corrupt")
        os.chmod(self.store, 0o600)
        MetaAdsOAuth.clear_store(self.store)
        self.assertFalse(self.store.exists())

    def test_validation_rejects_missing_tokens_invalid_expiry_write_scopes_and_unknown_fields(
        self,
    ) -> None:
        valid = {
            "version": 1,
            "client_id": "app-client-id",
            "client_secret": None,
            "access_token": "access",
            "refresh_token": "refresh",
            "access_expires_at": 1_700_003_600.0,
            "refresh_expires_at": None,
            "scopes": ["ads_read"],
            "issuer": META_ADS_MCP_RESOURCE,
            "resource": META_ADS_MCP_RESOURCE,
            "created_at": 1_700_000_000.0,
            "updated_at": 1_700_000_000.0,
        }
        for key, value in (
            ("access_token", ""),
            ("access_expires_at", float("nan")),
            ("scopes", ["ads_read", "ads_management"]),
            ("leaked_authorization_code", "sensitive"),
        ):
            payload = dict(valid)
            payload[key] = value
            encrypted = self._encrypt_payload(payload)
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_bytes(encrypted)
            os.chmod(self.store, 0o600)
            with self.assertRaisesRegex(OAuthError, "^oauth_credentials_invalid$"):
                self.oauth.load_credentials()

    def test_token_responses_may_not_reduce_requested_or_existing_scopes(self) -> None:
        request = self.oauth.authorization_url()
        request = replace(
            request, requested_scopes=frozenset({"ads_read", "ads_mcp_management"})
        )
        reduced = MetaAdsOAuth(
            client_id="app-client-id",
            store_path=self.store,
            key_path=self.key,
            metadata_fetcher=self._metadata,
            token_requester=lambda _url, _form: {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "ads_read",
            },
            now=lambda: 1_700_000_000.0,
        )
        with self.assertRaisesRegex(OAuthError, "^oauth_token_invalid$"):
            reduced.exchange_code("code", request)

    def test_token_response_may_not_grant_an_unrequested_scope(self) -> None:
        request = self.oauth.authorization_url()
        overgrant = MetaAdsOAuth(
            client_id="app-client-id",
            store_path=self.store,
            key_path=self.key,
            metadata_fetcher=self._metadata,
            token_requester=lambda _url, _form: {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "ads_read ads_mcp_management",
            },
            now=lambda: 1_700_000_000.0,
        )
        with self.assertRaisesRegex(OAuthError, "^oauth_token_invalid$"):
            overgrant.exchange_code("code", request)

    def test_post_form_never_follows_redirects(self) -> None:
        for redirect_status in (302, 307, 308):
            with self.subTest(redirect_status=redirect_status):
                target_requests: list[str] = []

                class _Redirect(BaseHTTPRequestHandler):
                    status = 0
                    target_port = 0

                    def do_POST(self) -> None:
                        self.send_response(self.status)
                        self.send_header(
                            "Location", f"http://127.0.0.1:{self.target_port}/target"
                        )
                        self.end_headers()

                    def log_message(self, _format: str, *_args: object) -> None:
                        return

                class _Target(BaseHTTPRequestHandler):
                    requests: ClassVar[list[str]] = []

                    def do_GET(self) -> None:
                        self.requests.append(self.path)
                        self.send_response(200)
                        self.end_headers()

                    def do_POST(self) -> None:
                        self.requests.append(
                            self.rfile.read(
                                int(self.headers["Content-Length"])
                            ).decode()
                        )
                        self.send_response(200)
                        self.end_headers()

                    def log_message(self, _format: str, *_args: object) -> None:
                        return

                with (
                    HTTPServer(("127.0.0.1", 0), _Target) as target,
                    HTTPServer(("127.0.0.1", 0), _Redirect) as redirect,
                ):
                    _Redirect.status = redirect_status
                    _Redirect.target_port = target.server_port
                    _Target.requests = target_requests
                    threads = [
                        threading.Thread(target=server.serve_forever, daemon=True)
                        for server in (target, redirect)
                    ]
                    for thread in threads:
                        thread.start()
                    try:
                        with self.assertRaises(HTTPError):
                            _post_form(
                                f"http://127.0.0.1:{redirect.server_port}/token",
                                {
                                    "client_secret": "must-not-leak",
                                    "code": "secret-code",
                                },
                            )
                        self.assertEqual(target_requests, [])
                    finally:
                        for server in (target, redirect):
                            server.shutdown()

    def test_rejects_non_default_https_ports(self) -> None:
        for key, value in (
            ("issuer", "https://mcp.facebook.com:444/ads"),
            ("authorization_endpoint", "https://www.facebook.com:8443/dialog/oauth"),
            ("token_endpoint", "https://graph.facebook.com:444/token"),
        ):
            with self.subTest(key=key):
                payload = dict(AUTH_METADATA)
                payload[key] = value
                oauth = MetaAdsOAuth(
                    client_id="client",
                    store_path=self.store,
                    key_path=self.key,
                    metadata_fetcher=lambda _url, payload=payload: payload,
                )
                with self.assertRaisesRegex(OAuthError, "^oauth_metadata_invalid$"):
                    oauth.discover()

    def test_private_files_and_directories_require_root_safe_modes_and_no_symlink(
        self,
    ) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="access",
            refresh_token="refresh",
            access_expires_at=1_700_003_600.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.store.parent.mkdir(mode=0o755)
        with self.assertRaisesRegex(OAuthError, "^oauth_credentials_unavailable$"):
            self.oauth.save_credentials(credentials)
        os.chmod(self.store.parent, 0o700)
        self.oauth.save_credentials(credentials)
        os.chmod(self.store, 0o640)
        with self.assertRaisesRegex(OAuthError, "^oauth_credentials_invalid$"):
            self.oauth.load_credentials()
        os.chmod(self.store, 0o600)
        real = self.store.parent
        linked = self.store.parent.with_name("linked-credentials")
        shutil.rmtree(real)
        linked.symlink_to(self.temp.name, target_is_directory=True)
        symlinked = MetaAdsOAuth(
            client_id="app-client-id",
            store_path=linked / "oauth.enc",
            key_path=self.key,
        )
        with self.assertRaisesRegex(OAuthError, "^oauth_credentials_unavailable$"):
            symlinked.save_credentials(credentials)

    def test_private_file_owner_and_descriptor_are_verified(self) -> None:
        credentials = OAuthCredentials(
            client_id="app-client-id",
            client_secret=None,
            access_token="access",
            refresh_token="refresh",
            access_expires_at=1_700_003_600.0,
            refresh_expires_at=None,
            scopes=frozenset({"ads_read"}),
            issuer=META_ADS_MCP_RESOURCE,
            resource=META_ADS_MCP_RESOURCE,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_000.0,
        )
        self.oauth.save_credentials(credentials)
        original_fstat = os.fstat
        calls = 0

        def changed_fstat(descriptor: int) -> os.stat_result:
            nonlocal calls
            calls += 1
            result = original_fstat(descriptor)
            if calls >= 2:
                values = list(result)
                values[4] = 1
                return os.stat_result(values)
            return result

        with (
            patch("brain.meta_ads_oauth.os.fstat", side_effect=changed_fstat),
            self.assertRaisesRegex(OAuthError, "^oauth_credentials_invalid$"),
        ):
            self.oauth.load_credentials()

    def test_callback_http_ignores_invalid_requests_until_valid_callback(self) -> None:
        request = self.oauth.authorization_url()
        callback = OAuthCallback(request)
        result: dict[str, object] = {}

        def serve() -> None:
            try:
                result["code"] = callback.serve_once(timeout_seconds=3)
            except Exception as exc:  # noqa: BLE001 - assertion captures worker outcome
                result["error"] = exc

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        deadline = time.monotonic() + 2
        while True:
            try:
                connection = HTTPConnection("127.0.0.1", 8766, timeout=1)
                connection.request("GET", "/oauth/callback?state=wrong&code=nope")
                self.assertEqual(connection.getresponse().status, 400)
                connection.close()
                break
            except ConnectionRefusedError:
                if time.monotonic() >= deadline:
                    self.fail("callback server did not start")
                time.sleep(0.02)
        connection = HTTPConnection("127.0.0.1", 8766, timeout=1)
        connection.request(
            "GET", f"/oauth/callback?state={request.state}&code=valid-code"
        )
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, {"code": "valid-code"})

    def _encrypt_payload(self, payload: dict[str, object]) -> bytes:
        # Test fixture format intentionally uses the public envelope encoding.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key.read_bytes()).encrypt(
            nonce,
            json.dumps(payload, allow_nan=True).encode(),
            b"brain-meta-ads-oauth-v1",
        )
        return json.dumps(
            {
                "version": 1,
                "nonce": base64.urlsafe_b64encode(nonce).decode(),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode(),
            }
        ).encode()
