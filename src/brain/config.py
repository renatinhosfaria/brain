from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_VALUES = {
    "openai_api_key": {"sk-..."},
    "github_token": {"ghp_..."},
    "brain_auth_token": {"gere-um-token-forte"},
    "brain_curator_token": {"..."},
    "brain_token_encryption_key": {"..."},
    "webhook_secret": {"gere-um-segredo"},
    "repo_url": {"https://github.com/usuario/brain-vault.git"},
}


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
    brain_curator_token: str
    brain_token_encryption_key: str

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

    # MCP rate limiting (0 = desabilitado). Limite por principal, por minuto.
    mcp_rate_limit_per_minute: int = 0

    # Reranking opcional do top-k vetorial via LLM (opt-in).
    rerank_enabled: bool = False
    rerank_candidates: int = 20

    @field_validator(
        "database_url",
        "openai_api_key",
        "github_token",
        "brain_auth_token",
        "brain_curator_token",
        "brain_token_encryption_key",
        "webhook_secret",
        "repo_url",
    )
    @classmethod
    def reject_example_placeholders(cls, value: str, info: ValidationInfo) -> str:
        if info.field_name == "database_url" and "troque-me" in value:
            raise ValueError("configure DATABASE_URL; valor de placeholder nao permitido")
        if value in _PLACEHOLDER_VALUES.get(info.field_name or "", set()):
            raise ValueError(f"configure {info.field_name}; valor de placeholder nao permitido")
        return value

    @field_validator("brain_token_encryption_key")
    @classmethod
    def validate_token_encryption_key(cls, value: str) -> str:
        try:
            Fernet(value.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("brain_token_encryption_key deve ser uma chave Fernet valida") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    # pydantic-settings popula os campos a partir do ambiente/.env em runtime.
    return Settings()  # type: ignore[call-arg]
