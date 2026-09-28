"""Typed settings loaded from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTEVAL_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agenteval:agenteval@localhost:55432/agenteval"
    redis_url: str = "redis://localhost:56379/0"

    # Queue
    dispatch_stream: str = "runs.dispatch"
    consumer_group: str = "workers"

    # Sandbox
    image_allowlist: list[str] = Field(default_factory=lambda: ["agenteval-sandbox:py312"])
    max_output_bytes: int = 64 * 1024
    workspace_tmpfs_mb: int = 64
    pids_limit: int = 128

    # Worker
    worker_id: str | None = None
    worker_concurrency: int = 2
    worker_block_ms: int = 5000
    worker_drain_timeout_s: int = 330

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
