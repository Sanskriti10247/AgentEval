"""Typed settings loaded from environment variables (see .env.example)."""

from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTEVAL_", env_file=".env", extra="ignore", populate_by_name=True
    )

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
    # Real model calls are opt-in: off by default, and never needed by the
    # standard test suite. The key is read by the worker only.
    enable_anthropic_provider: bool = False
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "AGENTEVAL_ANTHROPIC_API_KEY"),
    )
    # Global caps on agent-version limits. Versions above a cap are rejected
    # (422), never silently clamped: a version runs with exactly its config.
    agent_max_steps_cap: int = 100
    agent_max_total_tokens_cap: int = 2_000_000
    agent_max_cost_usd_cap: Decimal = Decimal("5.00")

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _empty_key_is_no_key(cls, v: object) -> object:
        # docker compose passes an unset ${ANTHROPIC_API_KEY:-} as "".
        return None if v == "" else v

    def enabled_providers(self) -> set[str]:
        """Providers agent versions may use. Shared by API (validation) and
        worker (wiring)."""
        enabled = set()
        if self.enable_fake_provider:
            enabled.add("fake")
        if self.enable_anthropic_provider:
            enabled.add("anthropic")
        return enabled

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
