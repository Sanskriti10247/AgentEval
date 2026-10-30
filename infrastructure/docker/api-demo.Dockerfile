# The public demo: the API, one worker, and a throwaway PostgreSQL 16 and Redis in ONE
# container, restored from infrastructure/demo/agenteval-demo.sql.gz on every start. No
# Docker daemon and no persistent disk: the worker runs tasks with the local sandbox backend
# (plain subprocesses, NOT an isolation boundary), which is only acceptable because public
# demo mode lets visitors launch nothing but the seeded agents and tasks. Built for a
# 512 MiB / 0.1 CPU free instance; see docs/deployment.md "Free hosting". Not for real use:
# Postgres runs with fsync off and everything is lost on restart.
# Build context: repository root.
FROM python:3.12-slim

# PostgreSQL 16 from the PostgreSQL project's Debian repository (Debian 13 itself ships 17;
# the stack and the snapshot are 16), redis-server and tini from Debian. No default cluster:
# the entrypoint creates its own under /tmp.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    install -d /usr/share/postgresql-common/pgdg /etc/postgresql-common/createcluster.d; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/demo.conf; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-16 redis-server tini; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /app

# API and worker share one environment. The Anthropic SDK is left out: the demo only has
# the scripted fake provider, and the worker imports the SDK only when it is enabled.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace \
        --no-install-package anthropic

COPY packages/core packages/core
COPY apps/api apps/api
COPY apps/worker apps/worker
RUN uv sync --frozen --no-dev --all-packages --no-install-package anthropic

COPY infrastructure/demo /app/demo

# Postgres and Redis listen on 127.0.0.1 only. Connections: API pool 3 + 2, worker pool
# 2 + 1 (set by the entrypoint), the lease keeper 1: under max_connections = 12.
# Task commands run with /usr/local/bin/python (3.12, as in the sandbox image), never the
# app's virtualenv: the local backend's PATH starts at /usr/local/bin.
ENV AGENTEVAL_PUBLIC_DEMO=true \
    AGENTEVAL_DEMO_MAX_REPETITIONS=3 \
    AGENTEVAL_DEMO_MAX_ACTIVE_RUNS=20 \
    AGENTEVAL_RATE_LIMIT_WRITE_PER_MIN=30 \
    AGENTEVAL_DATABASE_URL=postgresql+asyncpg://agenteval@127.0.0.1:5432/agenteval \
    AGENTEVAL_REDIS_URL=redis://127.0.0.1:6379/0 \
    AGENTEVAL_DB_POOL_SIZE=3 \
    AGENTEVAL_DB_MAX_OVERFLOW=2 \
    AGENTEVAL_STREAM_MAXLEN=1000 \
    AGENTEVAL_SANDBOX_BACKEND=local \
    AGENTEVAL_LEASE_KEEPER=thread \
    AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER=false \
    AGENTEVAL_WORKER_CONCURRENCY=1 \
    AGENTEVAL_WORKER_METRICS_PORT=0 \
    AGENTEVAL_WORKER_DRAIN_TIMEOUT_S=20 \
    AGENTEVAL_WORKER_CANCEL_GRACE_S=10

RUN useradd --uid 10001 --no-create-home app && chown app /app/demo
USER app
# The cluster with the snapshot already restored (entrypoint.sh --prepare): a start copies
# it instead of running initdb and the restore.
RUN /app/demo/entrypoint.sh --prepare
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/demo/entrypoint.sh"]
