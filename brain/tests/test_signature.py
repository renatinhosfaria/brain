import hashlib
import hmac

from brain.main import verify_signature
from brain.outbox import sign_webhook


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_assinatura_valida():
    body = b'{"ok":true}'
    assert verify_signature("seg", body, _sig("seg", body)) is True


def test_assinatura_invalida():
    assert verify_signature("seg", b"x", "sha256=deadbeef") is False


def test_header_ausente():
    assert verify_signature("seg", b"x", None) is False


def test_assinatura_outbox_usa_timestamp_ponto_e_body_bruto():
    body = b'{"note_id":"note-1","status":"created"}'
    timestamp = "2026-06-17T12:00:00+00:00"

    expected = "sha256=" + hmac.new(
        b"segredo",
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()

    assert sign_webhook("segredo", timestamp, body) == expected
