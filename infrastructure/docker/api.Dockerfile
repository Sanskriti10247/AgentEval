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

# Dependencies first (cached layer), then the workspace members.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/
RUN uv sync --frozen --no-dev --package agenteval-api --no-install-workspace

COPY packages/core packages/core
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --package agenteval-api

RUN useradd --uid 10001 --no-create-home app
USER app
EXPOSE 8000
CMD ["uvicorn", "agenteval_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
