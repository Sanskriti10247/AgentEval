"""Typed settings loaded from environment variables (see .env.example)."""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agenteval_core.reliability.fencing import LeaseTiming


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTEVAL_", env_file=".env", extra="ignore", populate_by_name=True
    )

    database_url: str = "postgresql+asyncpg://agenteval:agenteval@localhost:55432/agenteval"
    redis_url: str = "redis://localhost:56379/0"
    # SQLAlchemy connection pool per process. The defaults are SQLAlchemy's own; the
    # single-container demo image shrinks them to fit its small Postgres.
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    # Queue
    dispatch_stream: str = "runs.dispatch"
    # Step 7: evaluation ids travel on their own stream (same consumer group name).
    evaluation_stream: str = "evaluations.dispatch"
    consumer_group: str = "workers"

    # Browser origins the API answers CORS for (the dashboard). JSON list.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Authentication and rate limiting (ADR-012 A-C). Auth is ON unless explicitly
    # disabled (local experiments only). A rate budget is requests per minute per API
    # key (per client IP when auth is off); 0 disables that budget.
    auth_enabled: bool = True
    rate_limit_read_per_min: int = Field(default=600, ge=0)
    rate_limit_write_per_min: int = Field(default=120, ge=0)

    # Largest request body the API reads (bytes); bigger gets 413 before it is parsed. Must stay
    # above the biggest legitimate body (a task: 1 MB of files + a 2 MiB evaluation spec).
    max_request_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)

    # Read-only showcase (docs/deployment.md, "Free hosting"): every route that would
    # change state answers 403, reads need no API key, and nothing waits for a worker.
    read_only: bool = False
    # Public demo (docs/deployment.md, "Free hosting"): no API key, every caller is an
    # anonymous visitor limited per IP, and the only writes are launching evaluations and
    # runs of what already exists, cancelling and replaying. Repetitions per evaluation and
    # queued + running runs overall are capped. read_only wins if both are set.
    public_demo: bool = False
    demo_max_repetitions: int = Field(default=3, ge=1, le=10)
    demo_max_active_runs: int = Field(default=20, ge=1)

    # Sandbox. "docker" runs every task in its own container (ADR-003). "local" runs tasks
    # as plain subprocesses of the worker, for hosts without a Docker daemon: NOT an
    # isolation boundary, only for the public demo's seeded tasks (sandbox/local.py).
    # local_sandbox_dir holds its per-run directories (default: $TMPDIR or /tmp).
    sandbox_backend: Literal["docker", "local"] = "docker"
    local_sandbox_dir: str | None = None
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
    # Prometheus endpoint of the worker (ADR-010 A), served from a thread. 0
    # disables it. Loopback by default: compose sets 0.0.0.0 and publishes no
    # port, so only the compose network (Prometheus) can reach it.
    worker_metrics_port: int = Field(default=9464, ge=0, le=65535)
    worker_metrics_host: str = "127.0.0.1"

    # Leases (F7; ADR-008 D). Timing values come from the approved Step 1
    # plan: ttl 30 s, heartbeat 10 s, self-fence margin 10 s, reconciler tick
    # 5 s, worker liveness timeout 15 s. lease_timing() builds the validated
    # dataclass the keeper and the worker's self-fence math both use.
    lease_ttl_s: float = 30.0
    heartbeat_s: float = 10.0
    self_fence_margin_s: float = 10.0
    reconcile_interval_s: float = 5.0
    worker_liveness_timeout_s: float = 15.0
    # Where the lease keeper runs. "process" (ADR-008 D) keeps renewing even while the
    # worker's own event loop is blocked; "thread" saves the second interpreter (~40 MB)
    # on a small single-worker host such as the demo container.
    lease_keeper: Literal["process", "thread"] = "process"
    # Reconciler (Step 5): a QUEUED run, or a pending stream entry, idle this
    # long is republished / reclaimed; orphan containers are swept at this
    # interval; the stream is trimmed approximately to stream_maxlen entries.
    redispatch_after_s: float = 60.0
    orphan_sweep_interval_s: float = 300.0
    stream_maxlen: int = 100_000

    # Execution budget (F6): the max_attempts a run is pinned with when the request
    # doesn't give one (RunCreate, EvaluationCreate.run_max_attempts). 1..10.
    run_max_attempts: int = Field(default=3, ge=1, le=10)

    # Evaluation reliability (Step 7; F9). The grading budget an ad hoc run's
    # evaluation row is pinned with at creation (a batch run uses its batch's
    # eval_max_attempts instead); a FINISHED attempt still without an evaluation
    # row after a1_grace_s is recovered by the reconciler (A1).
    eval_max_attempts: int = Field(default=3, ge=1, le=10)
    a1_grace_s: float = Field(default=60.0, gt=0)

    # Artifacts (Step 6; ADR-008 A and E). Snapshot bytes live as files under
    # artifact_dir (a named volume shared by the workers); Postgres keeps the row.
    # insert_timeout bounds the metadata statement, save_deadline the gap from the
    # file's rename to that statement, and the sweeper deletes an unreferenced file
    # only once it is older than sweep_grace (validate_artifact_timing()).
    artifact_dir: str = "/var/lib/agenteval/artifacts"
    artifact_insert_timeout_s: float = 10.0
    artifact_save_deadline_s: float = 15.0
    artifact_sweep_grace_s: float = 120.0

    def validate_artifact_timing(self) -> None:
        """Startup check (ADR-008 E): the sweeper's grace must exceed the
        writer's deadline plus the statement timeout, or it could delete a file
        whose row is still about to be inserted."""
        for name in ("insert_timeout_s", "save_deadline_s", "sweep_grace_s"):
            if getattr(self, f"artifact_{name}") <= 0:
                raise ValueError(f"artifact_{name} must be positive")
        floor = self.artifact_save_deadline_s + self.artifact_insert_timeout_s
        if self.artifact_sweep_grace_s <= floor:
            raise ValueError(
                f"artifact_sweep_grace_s ({self.artifact_sweep_grace_s:g}) must exceed "
                f"artifact_save_deadline_s + artifact_insert_timeout_s ({floor:g})"
            )

    def lease_timing(self) -> LeaseTiming:
        return LeaseTiming(
            ttl_s=self.lease_ttl_s,
            heartbeat_s=self.heartbeat_s,
            margin_s=self.self_fence_margin_s,
            reconcile_interval_s=self.reconcile_interval_s,
            liveness_timeout_s=self.worker_liveness_timeout_s,
        )

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
