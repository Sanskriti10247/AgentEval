.PHONY: help sync sandbox-image build up down logs migrate test test-unit test-docker e2e lint fmt

COMPOSE := docker compose

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

e2e:  ## Full-stack test against the running compose stack (make up && make migrate first)
	uv run pytest tests/e2e -m e2e -v

lint:  ## Ruff lint + format check + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages/core/agenteval_core apps/api/agenteval_api apps/worker/agenteval_worker

fmt:  ## Autoformat
	uv run ruff format .
	uv run ruff check --fix .
