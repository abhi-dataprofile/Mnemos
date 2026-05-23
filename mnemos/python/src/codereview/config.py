"""Process configuration.

Settings are loaded from environment variables and the project ``.env`` file.
Defaults match ``.env.example`` at the repo root; production deployments
override via the environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level configuration for the orchestrator service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")

    # Embeddings (optional)
    voyage_api_key: str | None = Field(default=None, alias="VOYAGE_API_KEY")
    voyage_model: str = Field(default="voyage-code-3", alias="VOYAGE_MODEL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_embedding_model: str = Field(
        default="text-embedding-3-large", alias="OPENAI_EMBEDDING_MODEL"
    )

    # Service internals
    internal_secret: str = Field(default="dev-secret", alias="INTERNAL_SECRET")
    base_url: str = Field(default="http://localhost:3000", alias="BASE_URL")

    # Database + queue
    database_url: str = Field(
        default="postgresql+asyncpg://mnemos:mnemos@postgres:5432/mnemos",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # Observability
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    metrics_bearer_token: str | None = Field(default=None, alias="METRICS_BEARER_TOKEN")

    # Indexing
    index_max_commits: int = Field(default=1000, alias="INDEX_MAX_COMMITS")
    index_max_months: int = Field(default=12, alias="INDEX_MAX_MONTHS")

    # Per-PR budget caps
    per_pr_input_token_cap: int = Field(default=200_000, alias="PER_PR_INPUT_TOKEN_CAP")
    per_pr_output_token_cap: int = Field(default=20_000, alias="PER_PR_OUTPUT_TOKEN_CAP")

    # Agent enablement (comma-separated)
    enabled_agents: str = Field(
        default="conflict_detector,context_packager,reviewer_router",
        alias="MNEMOS_ENABLED_AGENTS",
    )

    # Logging
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    @property
    def enabled_agent_names(self) -> list[str]:
        return [name.strip() for name in self.enabled_agents.split(",") if name.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so we read the environment once per process."""

    return Settings()
