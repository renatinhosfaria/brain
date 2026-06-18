import pytest
from pathlib import Path
from cryptography.fernet import Fernet
from pydantic import ValidationError

from brain.config import Settings


def _valid_settings() -> dict:
    return {
        "database_url": "postgresql+asyncpg://u:p@h:5432/db",
        "openai_api_key": "sk-test",
        "github_token": "ghp_test",
        "brain_auth_token": "secret-token",
        "webhook_secret": "hmac-secret",
        "repo_url": "https://github.com/user/brain-vault.git",
    }


def test_settings_le_variaveis_de_ambiente(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("WEBHOOK_SECRET", "hmac-secret")
    monkeypatch.setenv("REPO_URL", "https://github.com/user/brain-vault.git")

    s = Settings()

    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.openai_api_key == "sk-test"
    assert s.embedding_model == "text-embedding-3-large"
    assert s.embedding_dim == 2000
    assert s.max_job_attempts == 5
    assert s.conversations_dir == "conversas"
    assert s.git_author_name == "brain-bot"


@pytest.mark.parametrize(
    ("field", "placeholder"),
    [
        ("database_url", "postgresql+asyncpg://brain:troque-me@postgres:5432/brain"),
        ("openai_api_key", "sk-..."),
        ("github_token", "ghp_..."),
        ("brain_auth_token", "gere-um-token-forte"),
        ("webhook_secret", "gere-um-segredo"),
        ("repo_url", "https://github.com/usuario/brain-vault.git"),
    ],
)
def test_settings_rejeita_placeholders_de_env_example(field, placeholder):
    kwargs = _valid_settings()
    kwargs[field] = placeholder

    with pytest.raises(ValidationError, match="placeholder"):
        Settings(**kwargs)


def test_defaults_de_modelos():
    s = Settings(**_valid_settings())
    assert s.extraction_model == "gpt-4o-mini"
    assert s.chunk_max_tokens == 512
    assert s.chunk_overlap_tokens == 64
    assert s.git_push_enabled is True


def test_settings_aceita_curadoria_ausente_durante_migracao():
    s = Settings(
        database_url="postgresql+asyncpg://x",
        openai_api_key="sk-test",
        github_token="ghp_test",
        brain_auth_token="legacy",
        webhook_secret="webhook",
        repo_url="https://example/repo.git",
    )
    assert s.brain_curator_slug == "hermes"
    assert s.brain_curator_name == "Hermes"
    assert s.brain_curator_token is None
    assert s.brain_token_encryption_key is None


def test_settings_curator_bootstrap_fields():
    key = Fernet.generate_key().decode()
    s = Settings(
        database_url="postgresql+asyncpg://x",
        openai_api_key="sk-test",
        github_token="ghp_test",
        brain_auth_token="legacy",
        webhook_secret="webhook",
        repo_url="https://example/repo.git",
        brain_curator_slug="hermes",
        brain_curator_name="Hermes",
        brain_curator_token="curator-token",
        brain_token_encryption_key=key,
    )
    assert s.brain_curator_slug == "hermes"
    assert s.brain_curator_name == "Hermes"
    assert s.brain_curator_token == "curator-token"
    assert s.brain_token_encryption_key == key


def test_env_example_documenta_curadoria_e_webhook_hermes():
    env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )

    for line in [
        "BRAIN_CURATOR_SLUG=hermes",
        "BRAIN_CURATOR_NAME=Hermes",
        "BRAIN_CURATOR_TOKEN=...",
        "BRAIN_TOKEN_ENCRYPTION_KEY=...",
        "HERMES_WEBHOOK_URL=",
        "HERMES_WEBHOOK_SECRET=",
    ]:
        assert line in env_example

    assert "BRAIN_TOKEN_ENCRYPTION_KEY must be a Fernet key" in env_example
    assert (
        'python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    ) in env_example
    assert (
        "BRAIN_AUTH_TOKEN is a migration fallback only when BRAIN_CURATOR_TOKEN is unset"
        in env_example
    )
    assert "not a parallel Hermes curator credential" in env_example
