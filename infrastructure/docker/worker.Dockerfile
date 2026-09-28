# Build context: repository root.
FROM python:3.12-slim

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
RUN uv sync --frozen --no-dev --package agenteval-worker --no-install-workspace

COPY packages/core packages/core
COPY apps/worker apps/worker
RUN uv sync --frozen --no-dev --package agenteval-worker

# Runs as root: it needs the mounted Docker socket, which is host-root
# equivalent anyway. The worker is a trusted component; sandboxes are not.
CMD ["python", "-m", "agenteval_worker.main"]
