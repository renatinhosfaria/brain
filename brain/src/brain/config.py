from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infra
    database_url: str
    openai_api_key: str
    github_token: str
    brain_auth_token: str
    webhook_secret: str
    repo_url: str

    # Curadoria / auth
    brain_curator_slug: str = "hermes"
    brain_curator_name: str = "Hermes"
    brain_curator_token: str | None = None
    brain_token_encryption_key: str | None = None

    # Agent inbox
    agent_inbox_dir: str = "_agents"

    # Hermes webhook
    hermes_webhook_url: str | None = None
    hermes_webhook_secret: str | None = None
    outbox_max_attempts: int = 8

    # Git
    repo_cache_path: str = "repo_cache"
    conversations_dir: str = "conversas"
    git_author_name: str = "brain-bot"
    git_author_email: str = "brain-bot@users.noreply.github.com"
    git_push_enabled: bool = True

    # IA
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 2000
    extraction_model: str = "gpt-4o-mini"

    # Indexação
    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64

    # Fila
    max_job_attempts: int = 5
    job_stale_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
