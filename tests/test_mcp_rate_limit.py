import pytest

from brain import auth
from brain.mcp import handlers


class _Settings:
    def __init__(self, per_minute: int) -> None:
        self.mcp_rate_limit_per_minute = per_minute


def test_require_curator_aplica_rate_limit_por_principal():
    handlers.configure_rate_limiter(_Settings(2))
    token = auth.set_current_principal(auth.Principal("curator", "hermes", "Hermes"))
    try:
        handlers._require_curator()
        handlers._require_curator()
        with pytest.raises(handlers.RateLimitExceeded):
            handlers._require_curator()
    finally:
        auth.reset_current_principal(token)
        handlers.configure_rate_limiter(_Settings(0))  # desabilita (estado global)


def test_rate_limit_desabilitado_por_padrao_nao_bloqueia():
    handlers.configure_rate_limiter(_Settings(0))
    token = auth.set_current_principal(auth.Principal("client", "codex", "Codex"))
    try:
        for _ in range(50):
            handlers._require_client()
    finally:
        auth.reset_current_principal(token)
