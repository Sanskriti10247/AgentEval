.PHONY: help sync sandbox-image build up down logs migrate test test-unit test-docker e2e lint fmt web web-check web-up

COMPOSE := docker compose
# The e2e suite runs the worker with short lease timings (docker-compose.e2e.yml).
COMPOSE_E2E := docker compose -f docker-compose.yml -f docker-compose.e2e.yml

help:  ## Show targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  %-14s %s\n", $$1, $$2}'

sync:  ## Install all workspace packages + dev tools into .venv
	uv sync --all-packages

sandbox-image:  ## Build the task sandbox image
	docker build -t agenteval-sandbox:py312 infrastructure/docker/sandbox

build: sandbox-image  ## Build api/worker images
	$(COMPOSE) build

up: sandbox-image  ## Start postgres, redis, api, worker
	$(COMPOSE) up -d --build --wait

down:  ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs:  ## Follow api + worker logs
	$(COMPOSE) logs -f api worker

migrate:  ## Apply database migrations (inside the api container)
	$(COMPOSE) run --rm api alembic -c packages/core/alembic.ini upgrade head

test: sandbox-image  ## Unit + integration + sandbox tests (needs Docker)
	uv run pytest

test-unit:  ## Tests that need no Docker at all
	uv run pytest packages/core/tests apps/worker/tests/test_bounded_buffer.py

e2e:  ## Full-stack tests against the compose stack (make up && make migrate first); recreates the worker with short lease timings
	$(COMPOSE_E2E) up -d --no-deps --wait worker
	@sleep 6
	uv run pytest tests/e2e -m e2e -v

lint:  ## Ruff lint + format check + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages/core/agenteval_core apps/api/agenteval_api apps/worker/agenteval_worker

fmt:  ## Autoformat
	uv run ruff format .
	uv run ruff check --fix .

web:  ## Run the dashboard dev server on :3000 (needs the API on :8000, e.g. make up)
	cd apps/web && npm ci && npm run dev

web-up:  ## Build and start the dashboard container on 127.0.0.1:3000 (compose profile "web")
	$(COMPOSE) --profile web up -d --build web

web-check:  ## Dashboard gates: typecheck, lint, test, build
	cd apps/web && npm ci && npm run typecheck && npm run lint && npm test && npm run build
