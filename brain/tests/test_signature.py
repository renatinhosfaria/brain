import hashlib
import hmac

from brain.main import verify_signature


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_assinatura_valida():
    body = b'{"ok":true}'
    assert verify_signature("seg", body, _sig("seg", body)) is True


def test_assinatura_invalida():
    assert verify_signature("seg", b"x", "sha256=deadbeef") is False


def test_header_ausente():
    assert verify_signature("seg", b"x", None) is False
