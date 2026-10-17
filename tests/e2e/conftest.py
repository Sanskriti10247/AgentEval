"""Shared fixtures for the E2E suite (the running compose stack).

    make up && make migrate && make e2e

The benchmark batches run once per session and are shared by the scenario
tests. The Postgres probe is strictly read-only (E1): SELECT/WITH only, a
read-only session default, and every query inside a read-only transaction.
"""

from collections.abc import Iterator

import asyncpg
import httpx
import pytest

from tests.e2e.bench import (
    BASE_URL,
    BENCH_TASKS,
    DATABASE_URL,
    ONLY_SUCCESS,
    Batch,
    ReadOnlyDB,
    all_results,
    api_headers,
    create_agent,
    create_dataset,
    start_evaluation,
    wait_complete,
)


@pytest.fixture(scope="session")
def stack() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        try:
            ready = c.get("/ready")
        except httpx.ConnectError:
            pytest.skip(f"API not reachable at {BASE_URL}; run `make up && make migrate`")
        assert ready.status_code == 200, ready.text
        c.headers.update(api_headers())
        yield c


@pytest.fixture(scope="session")
def db() -> ReadOnlyDB:
    probe = ReadOnlyDB(DATABASE_URL)
    try:
        probe.select("SELECT 1")
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"database probe unavailable at {DATABASE_URL}: {exc}")
    return probe


def _finish(stack: httpx.Client, batches: dict[str, Batch]) -> dict[str, Batch]:
    for batch in batches.values():
        wait_complete(stack, batch.evaluation_id)
        batch.rows = all_results(stack, batch.evaluation_id)
    return batches


@pytest.fixture(scope="session")
def benchmark(stack: httpx.Client) -> dict[str, Batch]:
    """S1: the six-task benchmark x {fixer (2 reps), quitter, cheater, looper},
    plus fixer again under custom weights (S3). All batches run concurrently
    on the stack's worker and are awaited together."""
    ds, tasks = create_dataset(stack, BENCH_TASKS)
    batches: dict[str, Batch] = {}
    for name, reps in (("fixer", 2), ("quitter", 1), ("cheater", 1), ("looper", 1)):
        version = create_agent(stack, name)
        ev = start_evaluation(stack, ds, version, repetitions=reps)
        batches[name] = Batch(name, ev["id"], tasks, reps)
    version = create_agent(stack, "fixer")
    ev = start_evaluation(stack, ds, version, scoring_config=ONLY_SUCCESS)
    batches["fixer-weights"] = Batch("fixer", ev["id"], tasks, 1)
    return _finish(stack, batches)


@pytest.fixture(scope="session")
def failures(stack: httpx.Client) -> dict[str, Batch]:
    """S5: platform and evaluator failures on a two-task dataset (T1, T3)."""
    ds, tasks = create_dataset(stack, ["T1", "T3"])
    batches = {
        name: Batch(name, start_evaluation(stack, ds, create_agent(stack, name))["id"], tasks, 1)
        for name in ("model-error", "hoarder")
    }
    return _finish(stack, batches)
