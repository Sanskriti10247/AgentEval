# ADR-004: Modular monolith + workers

**Status:** Accepted (vertical slice)

## Context
The original target diagram shows Agent, Task and Evaluation "services". Splitting them into network services now would add hops, deploys and distributed failure modes, and nothing needs them yet.

## Decision
One Python domain package (`agenteval_core`: models, schemas, repositories, services, queue, state machine) is used by two process types:
- `apps/api` (FastAPI): stateless request/response
- `apps/worker` (asyncio): long-running execution, scaled with `docker compose up --scale worker=N`

A uv workspace makes each of these a real installable package, so there are no `sys.path` hacks. This departs from the originally proposed layout, which puts `models/` under each app. That layout would duplicate the SQLAlchemy models and the state machine between the API and the worker.

Layering, enforced by convention and review:
- routes → services → repositories → models
- Routes contain no business logic.
- Repositories flush but never commit; services and the executor own transactions.

## Consequences
- There is one scaling knob, worker replicas, and the module boundaries stay in code, so a subsystem could be split out later if profiling shows a need.
- The API and worker images install only what they need: `--package agenteval-api` versus `--package agenteval-worker`.
