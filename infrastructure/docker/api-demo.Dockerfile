# The read-only demo: the API plus a throwaway PostgreSQL 16 and Redis in ONE container,
# restored from infrastructure/demo/agenteval-demo.sql.gz on every start. No worker, no
# sandboxes, no persistent disk. Built for a 512 MiB free instance; see docs/deployment.md
# "Free hosting (Vercel + Render)". Not for real use: Postgres runs with fsync off.
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

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/
RUN uv sync --frozen --no-dev --package agenteval-api --no-install-workspace

COPY packages/core packages/core
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --package agenteval-api

COPY infrastructure/demo /app/demo

# Postgres and Redis listen on 127.0.0.1 only; the pool stays well under max_connections.
ENV AGENTEVAL_READ_ONLY=true \
    AGENTEVAL_DATABASE_URL=postgresql+asyncpg://agenteval@127.0.0.1:5432/agenteval \
    AGENTEVAL_REDIS_URL=redis://127.0.0.1:6379/0 \
    AGENTEVAL_DB_POOL_SIZE=3 \
    AGENTEVAL_DB_MAX_OVERFLOW=2

RUN useradd --uid 10001 --no-create-home app
USER app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/demo/entrypoint.sh"]
