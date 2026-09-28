"""Typed settings loaded from environment variables (see .env.example)."""

from decimal import Decimal
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
    # At drain_timeout - cancel_grace, in-flight agent runs are cancelled
    # (reason worker_shutdown) and get cancel_grace to record and persist.
    worker_cancel_grace_s: int = 20

    # Agent runtime. The scripted fake provider is the default for dev, CI and
    # tests; real providers are opt-in.
    enable_fake_provider: bool = True
    # Global caps on agent-version limits. Versions above a cap are rejected
    # (422), never silently clamped: a version runs with exactly its config.
    agent_max_steps_cap: int = 100
    agent_max_total_tokens_cap: int = 2_000_000
    agent_max_cost_usd_cap: Decimal = Decimal("5.00")

    def enabled_providers(self) -> set[str]:
        """Providers agent versions may use. Shared by API (validation) and
        worker (wiring). No real provider is wired yet."""
        return {"fake"} if self.enable_fake_provider else set()

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
