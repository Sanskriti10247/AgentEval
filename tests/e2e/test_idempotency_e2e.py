"""S6 idempotency and S7 dataset/task rules through the running stack."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from tests.e2e.bench import (
    API,
    BASE_URL,
    MARKER,
    SPECS,
    ReadOnlyDB,
    all_results,
    api_headers,
    create_agent,
    create_dataset,
    ok,
    start_evaluation,
    task_body,
    wait_complete,
)

pytestmark = pytest.mark.e2e


def test_s6_one_batch_one_attempt_one_evaluation_per_run(
    stack: httpx.Client, db: ReadOnlyDB
) -> None:
    """Concurrent creates with one key make one batch. An immediate replay
    re-publishes the still-QUEUED runs (duplicate stream messages), yet every
    run is executed once and evaluated once."""
    ds, _ = create_dataset(stack, ["T1", "T3"])
    version = create_agent(stack, "fixer")
    key = f"e2e-{uuid.uuid4().hex}"

    def create(_: int) -> httpx.Response:
        with httpx.Client(base_url=BASE_URL, timeout=30, headers=api_headers()) as c:
            return c.post(
                f"{API}/evaluations",
                json={"name": "calc-bench", "dataset_id": ds, "agent_version_id": version,
                      "repetitions": 2},
                headers={"Idempotency-Key": key},
            )  # fmt: skip

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(create, range(5)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 202]
    [ev_id] = {r.json()["id"] for r in responses}
    # Replays re-dispatched QUEUED runs while the first dispatch was in flight.
    done = wait_complete(stack, ev_id)
    assert done["progress"]["runs_total"] == 4

    rows = all_results(stack, ev_id)
    for row in rows:
        run = ok(stack.get(f"{API}/runs/{row['run_id']}"), 200)
        assert run["attempt_count"] == 1, row["run_id"]
        assert row["evaluation"] is not None
    counts = db.select(
        """
        SELECT r.id, count(DISTINCT a.id) AS attempts, count(e.id) AS evaluations
        FROM runs r
        JOIN run_attempts a ON a.run_id = r.id
        LEFT JOIN attempt_evaluations e ON e.attempt_id = a.id
        WHERE r.evaluation_id = $1
        GROUP BY r.id
        """,
        uuid.UUID(ev_id),
    )
    assert len(counts) == 4
    assert all((c["attempts"], c["evaluations"]) == (1, 1) for c in counts)

    summary = ok(stack.get(f"{API}/evaluations/{ev_id}/summary"), 200)
    late = start_evaluation(stack, ds, version, repetitions=2, key=key, codes=(200,))
    assert late["id"] == ev_id and late["progress"] == done["progress"]
    assert ok(stack.get(f"{API}/evaluations/{ev_id}/summary"), 200) == summary


def test_s6_a_reused_key_for_a_different_request_is_rejected(stack: httpx.Client) -> None:
    ds, _ = create_dataset(stack, ["T3"])
    version = create_agent(stack, "fixer")
    key = f"e2e-{uuid.uuid4().hex}"
    first = start_evaluation(stack, ds, version, key=key)
    start_evaluation(stack, ds, version, repetitions=2, key=key, codes=(409,))
    wait_complete(stack, first["id"])


def test_s7_dataset_and_task_rules(stack: httpx.Client) -> None:
    ds, tasks = create_dataset(stack, ["T1"])
    assert ok(stack.post(f"{API}/datasets/{ds}/freeze"), 200)["status"] == "FROZEN"  # idempotent
    late = stack.post(f"{API}/tasks", json=task_body("T3", ds))
    assert late.status_code == 409

    draft = ok(stack.post(f"{API}/datasets", json={"name": f"d-{uuid.uuid4().hex[:8]}",
                                                   "version": "1"}), 201)  # fmt: skip
    ok(stack.post(f"{API}/tasks", json=task_body("T1", draft["id"])), 201)
    version = create_agent(stack, "fixer")
    start_evaluation(stack, draft["id"], version, codes=(409,))

    task = ok(stack.get(f"{API}/tasks/{tasks['T1']}"), 200)
    assert "evaluation" not in task and task["has_evaluation"] is True
    assert MARKER not in str(task)


@pytest.mark.parametrize(
    "spec",
    [
        {**SPECS["T1"], "hidden_files": {"conftest.py": MARKER}},  # type: ignore[dict-item]
        {"checks": [{"kind": "final_answer", "name": f"a{i}", "expected": MARKER * 2900}
                    for i in range(20)],
         "hidden_files": {"h.txt": MARKER * 45_000}},  # every field valid, > 2 MiB in total
    ],
    ids=["invalid", "over-2-mib"],
)  # fmt: skip
def test_s7_bad_specs_get_sanitised_errors(stack: httpx.Client, spec: dict[str, Any]) -> None:
    body = {**task_body("T1", ""), "dataset_id": None, "evaluation": spec}
    resp = stack.post(f"{API}/tasks", json=body)
    assert resp.status_code == 422, resp.text
    assert MARKER not in resp.text
