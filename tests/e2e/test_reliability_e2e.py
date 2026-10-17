"""Phase 5 reliability end to end, against the running compose stack.

    make up && make migrate && make e2e

`make e2e` recreates the worker with docker-compose.e2e.yml (lease ttl 15 s,
heartbeat 2 s, reconcile tick 1 s), so recovery takes seconds instead of
minutes. The crash scenarios SIGKILL the worker container with
`docker compose kill -s SIGKILL worker` and start it again with
`docker compose up -d worker`, always with the same two compose files so the
short timings survive the restart.

P1  worker crash mid-run: the run is recovered after the lease expires and
    completes as attempt 2; attempt 1 is ABANDONED (A3 + R9)
P2  a transient model failure that outlasts the in-step retries: attempt 1 ends
    RETRYING (R8), the backoff is respected, attempt 2 completes
P3  cancellation of a running run (CANCELLED, no evaluation, no artifact) and
    of a whole batch (every run terminal, batch complete)
P4  dead letter and replay: a non-retryable failure is FAILED + dead_letter_reason,
    listed by GET /runs?dead_letter=true, replayed, then completes
P5  duplicate dispatch is harmless: the same run id on the stream many times
    produces exactly one attempt
P6  worker crash during grading: the evaluation is recovered (E9a) and graded by
    the restarted worker; the agent attempt is not run again

The scripted model (FakeProvider) is deterministic per agent version. A scripted
error step with `"attempts": [1]` only fails attempt 1, which is how the same
version fails once and then succeeds.
"""

import os
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis

from tests.e2e.bench import (
    API,
    BUGGY,
    IMAGE,
    SPECS,
    VISIBLE_TESTS,
    ReadOnlyDB,
    create_agent,
    leftover_containers,
    ok,
    turn,
)

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.e2e.yml"]
REDIS_URL = os.environ.get("AGENTEVAL_E2E_REDIS_URL", "redis://127.0.0.1:56379/0")
DISPATCH_STREAM = "runs.dispatch"
CONSUMER_GROUP = "workers"
# docker-compose.e2e.yml: ttl 15 s, so recovery needs about 15 s plus a tick.
RECOVERY_S = 150


# -- helpers --------------------------------------------------------------------------------


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True)


def crash_worker() -> None:
    """SIGKILL: no drain, no lease release, no chance to write anything."""
    compose("kill", "-s", "SIGKILL", "worker")


def restart_worker() -> None:
    compose("up", "-d", "--no-deps", "--wait", "worker")


def wait_until(
    what: str, check: Callable[[], Any], timeout_s: float = RECOVERY_S, every_s: float = 0.25
) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(every_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


def get_run(http: httpx.Client, run_id: str) -> dict[str, Any]:
    return dict(ok(http.get(f"{API}/runs/{run_id}"), 200))


def wait_status(
    http: httpx.Client, run_id: str, *statuses: str, timeout_s: float = RECOVERY_S
) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        run = get_run(http, run_id)
        return run if run["status"] in statuses else None

    return dict(wait_until(f"run {run_id} to be {'/'.join(statuses)}", check, timeout_s))


def attempts_of(http: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    return list(ok(http.get(f"{API}/runs/{run_id}/attempts"), 200))


def events_of(http: httpx.Client, run_id: str, attempt_no: int = 1) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after = -1
    while True:
        page = ok(
            http.get(
                f"{API}/runs/{run_id}/attempts/{attempt_no}/events",
                params={"after_seq": after, "limit": 200},
            ),
            200,
        )
        out += page["events"]
        if page["next_after_seq"] is None:
            return out
        after = page["next_after_seq"]


def wait_agent_started(http: httpx.Client, run_id: str) -> None:
    """The runtime flushes run_started right before its first model call, so
    seeing it means the sandbox exists and the agent is in flight."""

    def check() -> bool:
        resp = http.get(f"{API}/runs/{run_id}/attempts/1/events", params={"limit": 5})
        return resp.status_code == 200 and any(
            e["event_type"] == "run_started" for e in resp.json()["events"]
        )

    wait_until(f"run {run_id} to start its agent", check, 90)


def make_task(
    http: httpx.Client,
    *,
    evaluation: dict[str, Any] | None = None,
    dataset_id: str | None = None,
    label: str = "rel",
) -> str:
    body: dict[str, Any] = {
        "name": f"e2e-{label}-{uuid.uuid4().hex[:8]}",
        "task_type": "agent",
        "image": IMAGE,
        "instructions": "The tests in test_calc.py fail. Fix calc.py so they pass.",
        "test_command": ["python", "-m", "unittest", "-q"],
        "files": {"calc.py": BUGGY, "test_calc.py": VISIBLE_TESTS},
        "timeout_s": 120,
        "evaluation": evaluation,
    }
    if dataset_id is not None:
        body["dataset_id"] = dataset_id
    return str(ok(http.post(f"{API}/tasks", json=body), 201)["id"])


def make_version(http: httpx.Client, script: list[dict[str, Any]]) -> str:
    agent = ok(http.post(f"{API}/agents", json={"name": f"e2e-rel-{uuid.uuid4().hex[:8]}"}), 201)
    version = ok(
        http.post(
            f"{API}/agents/{agent['id']}/versions",
            json={
                "model_provider": "fake",
                "model": "fake-model",
                "system_prompt": "You fix bugs in small Python projects.",
                "tools": ["read_file"],
                "limits": {"max_steps": 10},
                "model_settings": {"script": script},
            },
        ),
        201,
    )
    return str(version["id"])


def _slow_turn(delay_s: float, text: str = "done") -> dict[str, Any]:
    step = turn(text)
    step["delay_s"] = delay_s
    return step


def submit(http: httpx.Client, task_id: str, version_id: str, **extra: Any) -> dict[str, Any]:
    body = {"task_id": task_id, "agent_version_id": version_id, **extra}
    return dict(ok(http.post(f"{API}/runs", json=body), 202))


def no_leftovers(run_id: str) -> None:
    """Containers are removed by the worker, or by the reconciler after a crash."""
    wait_until(
        f"containers of run {run_id} to be gone",
        lambda: not leftover_containers({run_id}),
        timeout_s=60,
    )


# -- P5 duplicate dispatch ------------------------------------------------------------------


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _id_tuple(stream_id: str) -> tuple[int, int]:
    ms, _, seq = stream_id.partition("-")
    return int(ms), int(seq)


def test_p5_the_same_run_published_many_times_runs_once(
    stack: httpx.Client, db: ReadOnlyDB
) -> None:
    version = make_version(stack, [_slow_turn(4)])
    run = submit(stack, make_task(stack, label="p5"), version)
    run_id = run["id"]

    r = redis.Redis.from_url(REDIS_URL)
    try:
        dups = [r.xadd(DISPATCH_STREAM, {"run_id": run_id}) for _ in range(5)]
        wait_status(stack, run_id, "COMPLETED", timeout_s=90)
        dups += [r.xadd(DISPATCH_STREAM, {"run_id": run_id}) for _ in range(2)]
        last = max(_id_tuple(_text(d)) for d in dups)

        def handled() -> bool:
            group = next(
                g for g in r.xinfo_groups(DISPATCH_STREAM) if _text(g["name"]) == CONSUMER_GROUP
            )
            delivered = _id_tuple(_text(group["last-delivered-id"]))
            pending = r.xpending_range(
                DISPATCH_STREAM, CONSUMER_GROUP, min=_text(dups[0]), max=_text(dups[-1]),
                count=100,
            )  # fmt: skip
            return delivered >= last and not pending

        # Every duplicate was delivered and acknowledged (claim_skipped), none left pending.
        wait_until("the duplicate messages to be consumed and acked", handled, 60)
    finally:
        r.close()

    final = get_run(stack, run_id)
    assert (final["status"], final["attempt_count"]) == ("COMPLETED", 1)
    assert len(attempts_of(stack, run_id)) == 1
    assert [e["event_type"] for e in events_of(stack, run_id)].count("run_started") == 1
    rows = db.select("SELECT count(*) AS n FROM run_attempts WHERE run_id = $1::uuid", run_id)
    assert rows[0]["n"] == 1


# -- P4 dead letter and replay ------------------------------------------------------------------


def test_p4_a_non_retryable_failure_is_dead_lettered_then_replayed(stack: httpx.Client) -> None:
    task_id = make_task(stack, label="p4")
    version = make_version(
        stack,
        [{"type": "error", "kind": "authentication", "message": "scripted 401", "attempts": [1]},
         turn("recovered")],
    )  # fmt: skip
    run = submit(stack, task_id, version)  # default budget: 3 attempts
    assert run["max_attempts"] == 3

    # Non-retryable: dead-lettered after one attempt although two were left.
    failed = wait_status(stack, run["id"], "FAILED", timeout_s=90)
    assert (failed["outcome"], failed["dead_letter_reason"]) == ("MODEL_ERROR", "non_retryable")
    assert (failed["attempt_count"], failed["next_attempt_at"]) == (1, None)

    # The dead-letter view lists it; there is no DEAD_LETTER status.
    listed = ok(stack.get(f"{API}/runs", params={"dead_letter": "true", "task_id": task_id}), 200)
    assert [r["id"] for r in listed["runs"]] == [run["id"]]
    assert listed["next_after"] is None

    key = f"e2e-replay-{uuid.uuid4().hex}"
    replayed = ok(
        stack.post(f"{API}/runs/{run['id']}/replay", json={"additional_attempts": 2},
                   headers={"Idempotency-Key": key}),
        202,
    )  # fmt: skip
    assert (replayed["status"], replayed["max_attempts"], replayed["replay_count"]) == (
        "QUEUED", 5, 1,
    )  # fmt: skip
    assert replayed["dead_letter_reason"] is None and replayed["outcome"] is None
    # The same key again changes nothing.
    again = ok(
        stack.post(f"{API}/runs/{run['id']}/replay", json={"additional_attempts": 2},
                   headers={"Idempotency-Key": key}),
        200,
    )  # fmt: skip
    assert again["replay_count"] == 1

    done = wait_status(stack, run["id"], "COMPLETED", timeout_s=90)
    assert (done["outcome"], done["attempt_count"], done["replay_count"]) == (
        "AGENT_FINISHED", 2, 1,
    )  # fmt: skip
    attempts = attempts_of(stack, run["id"])
    assert [(a["attempt_no"], a["status"], a["outcome"]) for a in attempts] == [
        (1, "FINISHED", "MODEL_ERROR"),  # the history stays
        (2, "FINISHED", "AGENT_FINISHED"),
    ]  # fmt: skip
    after = ok(stack.get(f"{API}/runs", params={"dead_letter": "true", "task_id": task_id}), 200)
    assert after["runs"] == []
    # A COMPLETED run can't be replayed.
    ok(stack.post(f"{API}/runs/{run['id']}/replay", json={"additional_attempts": 1}), 409)


# -- P3 cancellation ----------------------------------------------------------------------------


def test_p3_cancelling_a_running_run(stack: httpx.Client, db: ReadOnlyDB) -> None:
    task_id = make_task(stack, evaluation=SPECS["T1"], label="p3")
    version = make_version(stack, [_slow_turn(60)])
    run = submit(stack, task_id, version)
    wait_agent_started(stack, run["id"])

    flagged = ok(stack.post(f"{API}/runs/{run['id']}/cancel"), 202)
    assert flagged["cancel_requested_at"] is not None and flagged["cancel_reason"] == "user"
    assert flagged["status"] in ("RUNNING", "CANCELLED")

    # The worker learns of the flag on its next lease heartbeat (2 s here).
    cancelled = wait_status(stack, run["id"], "CANCELLED", timeout_s=60)
    assert cancelled["finished_at"] is not None
    [attempt] = attempts_of(stack, run["id"])
    assert attempt["status"] == "CANCELLED" and attempt["outcome"] is None
    ok(stack.get(f"{API}/runs/{run['id']}/attempts/1/evaluation"), 404)  # no evaluation
    counts = db.select(
        "SELECT (SELECT count(*) FROM attempt_artifacts x WHERE x.attempt_id = a.id) AS artifacts,"
        " (SELECT count(*) FROM attempt_evaluations x WHERE x.attempt_id = a.id) AS evaluations,"
        " a.failure_reason, a.workspace_capture"
        " FROM run_attempts a WHERE a.run_id = $1::uuid",
        run["id"],
    )
    assert counts == [
        {"artifacts": 0, "evaluations": 0, "failure_reason": "cancelled_by_user",
         "workspace_capture": None}
    ]  # fmt: skip
    no_leftovers(run["id"])
    # A repeat is a no-op, not an error.
    assert ok(stack.post(f"{API}/runs/{run['id']}/cancel"), 200)["status"] == "CANCELLED"


def test_p3_cancelling_a_batch_leaves_every_run_terminal(stack: httpx.Client) -> None:
    ds = ok(stack.post(f"{API}/datasets", json={"name": f"p3-{uuid.uuid4().hex[:8]}",
                                                "version": "1"}), 201)  # fmt: skip
    for _ in range(3):  # more runs than the worker's concurrency (2): some stay QUEUED
        make_task(stack, dataset_id=ds["id"], label="p3batch")
    ok(stack.post(f"{API}/datasets/{ds['id']}/freeze"), 200)
    version = make_version(stack, [_slow_turn(60)])
    batch = ok(
        stack.post(f"{API}/evaluations", json={"name": "p3-batch", "dataset_id": ds["id"],
                                               "agent_version_id": version}),
        202,
    )  # fmt: skip
    ev = batch["id"]

    def one_running() -> bool:
        runs = ok(stack.get(f"{API}/runs", params={"evaluation_id": ev, "status": "RUNNING"}), 200)
        return bool(runs["runs"])

    wait_until("a batch run to be RUNNING", one_running, 90)
    cancelled = ok(stack.post(f"{API}/evaluations/{ev}/cancel"), 202)
    assert cancelled["cancel_requested_at"] is not None

    def complete() -> dict[str, Any] | None:
        body = ok(stack.get(f"{API}/evaluations/{ev}"), 200)
        return body if body["progress"]["complete"] else None

    final = wait_until("the cancelled batch to complete", complete, 90)
    progress = final["progress"]
    assert progress["runs_by_status"]["CANCELLED"] == 3
    assert sum(progress["evaluations_by_status"].values()) == 0  # cancelled runs: no evaluation
    runs = ok(stack.get(f"{API}/runs", params={"evaluation_id": ev}), 200)["runs"]
    assert [r["status"] for r in runs] == ["CANCELLED"] * 3
    assert {r["cancel_reason"] for r in runs} == {"batch"}
    for r in runs:
        no_leftovers(r["id"])


# -- P2 transient failure retried --------------------------------------------------------------


def test_p2_a_transient_model_failure_is_retried_with_backoff(
    stack: httpx.Client, db: ReadOnlyDB
) -> None:
    # 4 errors = the first call plus the 3 in-step retries (about 15 s of backoff),
    # so attempt 1 ends MODEL_ERROR/model_overloaded. Attempt 2 skips them all.
    errors = [{"type": "error", "kind": "overloaded", "message": f"scripted overload {i}",
               "attempts": [1]} for i in range(4)]  # fmt: skip
    version = make_version(stack, [*errors, turn("worked on the second attempt")])
    run = submit(stack, make_task(stack, label="p2"), version)

    seen: set[str] = set()

    def finished() -> dict[str, Any] | None:
        current = get_run(stack, run["id"])
        seen.add(current["status"])
        if current["status"] == "RETRYING":
            assert current["next_attempt_at"] is not None and current["outcome"] is None
            assert current["error_message"] is None  # R8: RETRYING carries no outcome or message
        return current if current["status"] == "COMPLETED" else None

    done = wait_until("the retried run to complete", finished, 120)
    assert (done["attempt_count"], done["outcome"]) == (2, "AGENT_FINISHED")
    a1, a2 = attempts_of(stack, run["id"])
    assert (a1["status"], a1["outcome"]) == ("FINISHED", "MODEL_ERROR")
    assert (a2["status"], a2["outcome"]) == ("FINISHED", "AGENT_FINISHED")

    # Attempt 1 recorded every in-step retry, then gave up.
    model_errors = [e for e in events_of(stack, run["id"], 1) if e["event_type"] == "model_error"]
    assert [e["output"]["decision"] for e in model_errors] == ["retry"] * 3 + ["give_up"]
    assert model_errors[-1]["output"]["give_up_reason"] == "retries_exhausted"
    assert not [e for e in events_of(stack, run["id"], 2) if e["event_type"] == "model_error"]

    # The structured reason, not the message, made it retryable (F4).
    rows = db.select(
        "SELECT attempt_no, failure_reason, retryable FROM run_attempts"
        " WHERE run_id = $1::uuid ORDER BY attempt_no",
        run["id"],
    )
    assert rows == [
        {"attempt_no": 1, "failure_reason": "model_overloaded", "retryable": True},
        {"attempt_no": 2, "failure_reason": None, "retryable": None},
    ]

    # Backoff after attempt 1 is 2 s x U(0.8, 1.2): attempt 2 can't start before 1.6 s.
    gap = (_ts(a2["started_at"]) - _ts(a1["finished_at"])).total_seconds()
    assert gap >= 1.5, gap


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# -- P1 worker crash mid-run -------------------------------------------------------------------


def test_p1_a_run_survives_a_worker_crash(stack: httpx.Client, db: ReadOnlyDB) -> None:
    version = make_version(stack, [_slow_turn(15, "finished after the crash")])
    run = submit(stack, make_task(stack, label="p1"), version)
    wait_agent_started(stack, run["id"])
    assert get_run(stack, run["id"])["status"] == "RUNNING"

    crash_worker()
    restart_worker()

    done = wait_status(stack, run["id"], "COMPLETED", timeout_s=RECOVERY_S)
    assert (done["attempt_count"], done["outcome"]) == (2, "AGENT_FINISHED")
    a1, a2 = attempts_of(stack, run["id"])
    # A3: the reconciler abandoned the dead worker's attempt (lease expired).
    assert (a1["status"], a1["outcome"]) == ("ABANDONED", "INTERNAL_ERROR")
    assert (a2["status"], a2["outcome"]) == ("FINISHED", "AGENT_FINISHED")
    assert a1["worker_id"] != a2["worker_id"]  # the restarted worker has a new identity
    rows = db.select(
        "SELECT attempt_no, failure_reason, retryable FROM run_attempts"
        " WHERE run_id = $1::uuid ORDER BY attempt_no",
        run["id"],
    )
    assert rows == [
        {"attempt_no": 1, "failure_reason": "lease_expired", "retryable": True},
        {"attempt_no": 2, "failure_reason": None, "retryable": None},
    ]
    no_leftovers(run["id"])  # the dead worker's sandbox was killed by the reconciler


# -- P6 worker crash during grading ---------------------------------------------------------------

SLOW_CHECK_SPEC = {
    "checks": [{
        "kind": "command", "name": "slow_check", "trusted_environment": True, "timeout_s": 60,
        "argv": ["python", "-c",
                 "import sys, time, calc; time.sleep(10); sys.exit(calc.add(1, 1) != 2)"],
    }],
}  # fmt: skip


def test_p6_grading_survives_a_worker_crash(stack: httpx.Client, db: ReadOnlyDB) -> None:
    task_id = make_task(stack, evaluation=SLOW_CHECK_SPEC, label="p6")
    version = create_agent(stack, "fixer")
    run = submit(stack, task_id, version)

    q = (
        "SELECT e.status, e.eval_attempts, e.worker_id, e.error_reason FROM attempt_evaluations e"
        " JOIN run_attempts a ON a.id = e.attempt_id WHERE a.run_id = $1::uuid"
    )

    def grading() -> dict[str, Any] | None:
        rows = db.select(q, run["id"])
        return rows[0] if rows and rows[0]["status"] == "RUNNING" else None

    first = wait_until("the evaluation to be claimed", grading, 120, every_s=0.1)
    assert first["eval_attempts"] == 1
    crash_worker()
    restart_worker()

    def graded() -> dict[str, Any] | None:
        resp = stack.get(f"{API}/runs/{run['id']}/attempts/1/evaluation")
        return (
            resp.json()
            if resp.status_code == 200 and resp.json()["status"] == "SUCCEEDED"
            else None
        )

    public = wait_until("the evaluation to be graded again", graded, RECOVERY_S)
    assert public["task_success"] is True and public["failure_category"] == "passed"

    [row] = db.select(q, run["id"])
    # E9a put it back to PENDING (consuming the first grading attempt); the
    # restarted worker's claim was the second, and success cleared the reason (G13).
    assert (row["status"], row["eval_attempts"], row["error_reason"]) == ("SUCCEEDED", 2, None)
    assert row["worker_id"] != first["worker_id"]
    # The agent attempt was not run again, and the run is untouched.
    final = get_run(stack, run["id"])
    assert (final["status"], final["attempt_count"]) == ("COMPLETED", 1)
    assert len(attempts_of(stack, run["id"])) == 1
    no_leftovers(run["id"])
