"""Evaluation batches through the API (Phase 4 step 6): creation and
idempotency, progress, results, summary (D7 semantics), compare, and the
per-attempt public evaluation. Results are seeded as the worker leaves them."""

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from agenteval_core.config import Settings
from agenteval_core.evaluation.scoring import (
    DEFAULT_SCORING_CONFIG,
    ScoringConfig,
    ScoringWeights,
)
from agenteval_core.evaluation.stats import wilson_interval
from agenteval_core.models import Evaluation, Run, Task
from agenteval_core.queue.streams import RunQueue
from tests.integration.eval_seed import (
    SM,
    agent_version,
    create_evaluation,
    frozen_dataset,
    seed,
)

ONLY_SUCCESS = ScoringConfig(
    weights=ScoringWeights(
        task_success=Decimal(1),
        test_score=Decimal(0),
        reliability=Decimal(0),
        efficiency=Decimal(0),
    )
)


async def stream_len(redis: Redis, settings: Settings) -> int:
    return int(await redis.xlen(settings.dispatch_stream))


async def run_count(sm: SM, evaluation_id: str) -> int:
    async with sm() as s:
        stmt = select(func.count()).where(Run.evaluation_id == uuid.UUID(evaluation_id))
        return int((await s.execute(stmt)).scalar_one())


# -- creation and idempotency ---------------------------------------------------------------------


async def test_a_batch_creates_every_cell_and_dispatches_it(
    client: httpx.AsyncClient, sessionmaker: SM, redis: Redis, settings: Settings
) -> None:
    ds, tasks = await frozen_dataset(client)
    version = await agent_version(sessionmaker)
    resp = await create_evaluation(client, ds, version, repetitions=2)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["scoring_config_hash"] == DEFAULT_SCORING_CONFIG.config_hash()
    assert ScoringConfig.model_validate(body["scoring_config"]) == DEFAULT_SCORING_CONFIG
    progress = body["progress"]
    assert progress["runs_total"] == 6 and progress["runs_by_status"]["QUEUED"] == 6
    assert progress["complete"] is False
    assert await stream_len(redis, settings) == 6
    async with sessionmaker() as s:
        keys = (
            await s.execute(select(Run.idempotency_key).where(Run.evaluation_id == body["id"]))
        ).scalars()
        assert sorted(keys) == sorted(
            f"eval:{body['id']}:{t}:{rep}" for t in tasks.values() for rep in (1, 2)
        )


async def test_idempotency_key_replays_conflicts_and_races(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    ds, _ = await frozen_dataset(client)
    version = await agent_version(sessionmaker)
    first = await create_evaluation(client, ds, version, key="k1")
    replay = await create_evaluation(client, ds, version, key="k1")
    assert (first.status_code, replay.status_code) == (202, 200)
    assert replay.json()["id"] == first.json()["id"]
    assert await run_count(sessionmaker, first.json()["id"]) == 9

    other = await create_evaluation(client, ds, version, key="k1", repetitions=1)
    assert other.status_code == 409
    other_config = await create_evaluation(
        client, ds, version, key="k1", scoring_config=ONLY_SUCCESS
    )
    assert other_config.status_code == 409

    racing = await asyncio.gather(
        *(create_evaluation(client, ds, version, key="k2") for _ in range(5))
    )
    assert sorted(r.status_code for r in racing) == [200, 200, 200, 200, 202]
    [batch_id] = {r.json()["id"] for r in racing}
    assert await run_count(sessionmaker, batch_id) == 9


async def test_batch_preconditions(client: httpx.AsyncClient, sessionmaker: SM) -> None:
    version = await agent_version(sessionmaker)
    draft = (await client.post("/api/v1/datasets", json={"name": "d", "version": "1"})).json()
    assert (await create_evaluation(client, draft["id"], version)).status_code == 409
    ds, _ = await frozen_dataset(client)
    assert (await create_evaluation(client, ds, uuid.uuid4())).status_code == 404
    assert (await create_evaluation(client, str(uuid.uuid4()), version)).status_code == 404
    bad_weights = {"version": "v1", "weights": {"task_success": "0.5", "test_score": "0.1",
                   "reliability": "0.1", "efficiency": "0.1"}}  # fmt: skip
    resp = await client.post(
        "/api/v1/evaluations",
        json={"name": "x", "dataset_id": ds, "agent_version_id": str(version),
              "scoring_config": bad_weights},
    )  # fmt: skip
    assert resp.status_code == 422


async def test_more_than_1000_runs_is_refused(client: httpx.AsyncClient, sessionmaker: SM) -> None:
    ds = (await client.post("/api/v1/datasets", json={"name": "big", "version": "1"})).json()["id"]
    async with sessionmaker() as s:
        s.add_all(
            Task(name=f"t{i}", task_type="agent", image="img", instructions="do", files={},
                 timeout_s=60, memory_mb=256, cpu_limit=1.0, dataset_id=uuid.UUID(ds))
            for i in range(101)
        )  # fmt: skip
        await s.commit()
    await client.post(f"/api/v1/datasets/{ds}/freeze")
    version = await agent_version(sessionmaker)
    resp = await create_evaluation(client, ds, version, repetitions=10)
    assert resp.status_code == 422
    async with sessionmaker() as s:  # nothing was written
        assert (await s.execute(select(func.count()).select_from(Evaluation))).scalar_one() == 0


async def test_failed_dispatch_is_503_and_a_replay_redispatches(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    redis: Redis,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ds, _ = await frozen_dataset(client)
    version = await agent_version(sessionmaker)

    async def down(self: RunQueue, run_id: uuid.UUID) -> None:
        raise RedisConnectionError("redis is down")

    with monkeypatch.context() as m:
        m.setattr(RunQueue, "publish", down)
        failed = await create_evaluation(client, ds, version, key="k-dispatch")
    assert failed.status_code == 503 and failed.headers["Retry-After"] == "1"
    assert await stream_len(redis, settings) == 0
    replay = await create_evaluation(client, ds, version, key="k-dispatch")
    assert replay.status_code == 200
    assert await stream_len(redis, settings) == 9


# -- reading results -------------------------------------------------------------------------------


async def test_summary_follows_d7_and_r7(client: httpx.AsyncClient, sessionmaker: SM) -> None:
    s = await seed(client, sessionmaker)
    summary = (await client.get(f"/api/v1/evaluations/{s.evaluation_id}/summary")).json()
    assert (summary["attempts"], summary["graded"], summary["passed"]) == (7, 3, 1)
    assert summary["success_rate"] == pytest.approx(1 / 3)
    assert summary["ci95"] == pytest.approx(list(wilson_interval(1, 3) or ()))
    assert summary["platform_failures"] == 2  # B1 (SKIPPED row) + B3 (no attempt)
    assert summary["platform_failure_rate"] == pytest.approx(2 / 7)
    assert (summary["evaluation_errors"], summary["ungraded"], summary["in_progress"]) == (1, 1, 2)
    assert summary["complete"] is False
    assert summary["failure_categories"] == {
        "evaluation_error": 1, "passed": 1, "platform_internal": 1,
        "platform_sandbox": 1, "tampering": 1, "tests_failed": 1,
    }  # fmt: skip
    assert Decimal(summary["mean_score"]["overall"]) == Decimal("0.40000")
    assert summary["latency_ms"] == {"p50": 300, "p95": 600}  # nearest rank over 6 attempts
    assert Decimal(summary["cost_usd"]["total"]) == Decimal("0.06")
    assert summary["tokens"] == {"tokens_in": 600, "tokens_out": 120}
    per_task = {t["task_id"]: t for t in summary["per_task"]}
    a, b, c = (per_task[s.tasks[k]] for k in "ABC")
    assert (a["attempts"], a["graded"], a["passed"]) == (3, 3, 1)
    assert a["success_rate"] == pytest.approx(1 / 3) and a["task_name"] == "task-A"
    assert (b["graded"], b["success_rate"]) == (0, None)
    assert (c["attempts"], c["graded"], c["success_rate"]) == (1, 0, None)


async def test_progress_counts(client: httpx.AsyncClient, sessionmaker: SM) -> None:
    s = await seed(client, sessionmaker)
    progress = (await client.get(f"/api/v1/evaluations/{s.evaluation_id}")).json()["progress"]
    assert progress["runs_total"] == 9
    assert progress["runs_by_status"] == {
        "QUEUED": 1, "CLAIMED": 0, "RUNNING": 0, "RETRYING": 0, "COMPLETED": 6, "FAILED": 2,
        "CANCELLED": 0,
    }  # fmt: skip
    assert progress["evaluations_by_status"] == {
        "PENDING": 0, "RUNNING": 1, "SUCCEEDED": 3, "ERROR": 1, "SKIPPED": 2,
    }  # fmt: skip
    assert progress["awaiting_evaluation"] == 1  # C2
    assert progress["complete"] is False


async def test_results_are_public_and_keyset_paged(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)
    url = f"/api/v1/evaluations/{s.evaluation_id}/results"
    rows, after, pages = [], None, 0
    while True:
        params: dict[str, str | int] = {"limit": 4}
        if after:
            params["after"] = after
        page = (await client.get(url, params=params)).json()
        rows += page["results"]
        pages += 1
        after = page["next_after"]
        if after is None:
            break
    assert pages == 3 and len(rows) == 9
    assert len({r["run_id"] for r in rows}) == 9
    by_run = {uuid.UUID(r["run_id"]): r for r in rows}
    a1 = by_run[s.runs["A1"]]["evaluation"]
    assert a1["status"] == "SUCCEEDED" and a1["failure_category"] == "passed"
    assert a1["checks"] == [{"position": 1, "passed": True, "score": 1.0, "duration_ms": 7}]
    assert Decimal(a1["score"]["overall"]) == Decimal("0.9")
    assert by_run[s.runs["A3"]]["evaluation"]["score"]["integrity_gate_applied"] is True
    assert by_run[s.runs["B2"]]["evaluation"]["status"] == "ERROR"
    assert by_run[s.runs["B3"]]["evaluation"] is None and by_run[s.runs["B3"]]["attempt_no"] is None
    assert by_run[s.runs["C3"]]["run_status"] == "QUEUED"
    assert (await client.get(url, params={"after": "nope"})).status_code == 422


async def test_attempt_evaluation_endpoint(client: httpx.AsyncClient, sessionmaker: SM) -> None:
    s = await seed(client, sessionmaker)
    ok = await client.get(f"/api/v1/runs/{s.runs['A1']}/attempts/1/evaluation")
    assert ok.status_code == 200
    body = ok.json()
    assert set(body) == {
        "status", "task_success", "failure_category", "tags", "checks", "score", "metrics",
        "attempt_no", "created_at", "finished_at",
    }  # fmt: skip
    assert body["failure_category"] == "passed" and body["attempt_no"] == 1
    running = (await client.get(f"/api/v1/runs/{s.runs['C2']}/attempts/1/evaluation")).json()
    assert running["status"] == "RUNNING" and running["failure_category"] is None
    assert (
        await client.get(f"/api/v1/runs/{s.runs['B3']}/attempts/1/evaluation")
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/runs/{uuid.uuid4()}/attempts/1/evaluation")
    ).status_code == 404


async def test_compare_pairs_tasks_and_refuses_mismatches(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)
    other = await create_evaluation(client, s.dataset_id, s.version_id, repetitions=1)
    other_id = other.json()["id"]
    compare = await client.get(
        "/api/v1/evaluations/compare", params={"ids": f"{s.evaluation_id},{other_id}"}
    )
    assert compare.status_code == 200, compare.text
    body = compare.json()
    assert body["dataset_id"] == s.dataset_id
    assert [e["evaluation_id"] for e in body["evaluations"]] == [s.evaluation_id, other_id]
    a = next(t for t in body["per_task"] if t["task_id"] == s.tasks["A"])
    assert a["success_rate"][s.evaluation_id] == pytest.approx(1 / 3)
    assert a["success_rate"][other_id] is None and a["delta_vs_first"][other_id] is None
    assert a["delta_vs_first"][s.evaluation_id] == 0

    different_config = await create_evaluation(
        client, s.dataset_id, s.version_id, scoring_config=ONLY_SUCCESS
    )
    ds2, _ = await frozen_dataset(client)
    different_dataset = await create_evaluation(client, ds2, s.version_id)
    for bad in (different_config, different_dataset):
        ids = f"{s.evaluation_id},{bad.json()['id']}"
        resp = await client.get("/api/v1/evaluations/compare", params={"ids": ids})
        assert resp.status_code == 422
    for ids in (s.evaluation_id, f"{s.evaluation_id},{s.evaluation_id}", "not-a-uuid,x"):
        resp = await client.get("/api/v1/evaluations/compare", params={"ids": ids})
        assert resp.status_code == 422
