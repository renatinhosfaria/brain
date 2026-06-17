import pytest
from cryptography.fernet import Fernet

from brain import auth
from brain.auth import verify_bearer_token, AuthError


def test_token_valido_passa():
    verify_bearer_token("Bearer secret", expected="secret")  # não levanta


def test_token_invalido_falha():
    with pytest.raises(AuthError):
        verify_bearer_token("Bearer errado", expected="secret")


def test_header_ausente_falha():
    with pytest.raises(AuthError):
        verify_bearer_token(None, expected="secret")


def test_formato_sem_bearer_falha():
    with pytest.raises(AuthError):
        verify_bearer_token("secret", expected="secret")


def test_token_crypto_roundtrip():
    key = Fernet.generate_key().decode()
    token = auth.generate_client_token("chatgpt-web")
    encrypted = auth.encrypt_token(token, key)
    assert encrypted != token
    assert auth.decrypt_token(encrypted, key) == token
    assert auth.hash_token(token) == auth.hash_token(token)


def test_principal_context_roundtrip():
    principal = auth.Principal(type="client", slug="chatgpt-web", name="ChatGPT Web")
    token = auth.set_current_principal(principal)
    try:
        assert auth.get_current_principal() == principal
    finally:
        auth.reset_current_principal(token)
