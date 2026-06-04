import pytest

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
