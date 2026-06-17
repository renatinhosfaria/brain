from cryptography.fernet import Fernet

from brain.config import Settings


def test_settings_le_variaveis_de_ambiente(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("BRAIN_CURATOR_TOKEN", "curator-token")
    monkeypatch.setenv("BRAIN_TOKEN_ENCRYPTION_KEY", key)
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


def test_defaults_de_modelos():
    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        openai_api_key="sk",
        github_token="gh",
        brain_auth_token="t",
        brain_curator_token="curator-token",
        brain_token_encryption_key=Fernet.generate_key().decode(),
        webhook_secret="w",
        repo_url="https://x/y.git",
    )
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
