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
