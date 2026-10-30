"""Provider-aware launching and live tracking: GET /providers, POST /evaluations/estimate,
the evaluation budget (stored on create, enforced by the worker), the public demo's
simulated-only rule, evaluation context and comparison notes, and GET /stats/activity.
FakeProvider only: no test here calls a real model."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agenteval_api.main import create_app
from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.config import Settings
from agenteval_core.evaluation.scoring import DEFAULT_SCORING_CONFIG
from agenteval_core.models import Agent, AgentVersion, Evaluation, Run, RunAttempt
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.runs import RunRepository
from agenteval_worker.sandbox.local import LocalSandbox
from tests.integration.conftest import bearer
from tests.integration.eval_seed import NOW as SEEDED_AT
from tests.integration.eval_seed import (
    SM,
    agent_version,
    create_evaluation,
    frozen_dataset,
    seed,
)
from tests.integration.test_agent_executor import FIX_THE_BUG, handle_next
from tests.integration.test_evaluation_runner import RecordingFake, make_run
from tests.integration.test_local_backend import PATH, build
from tests.integration.test_public_demo import client_for, demo_app

API = "/api/v1"


def hour_of(t: datetime | str) -> int:
    stamp = datetime.fromisoformat(t) if isinstance(t, str) else t
    return int(stamp.timestamp()) // 3600


async def real_version(sm: SM, model: str = "claude-haiku-4-5") -> uuid.UUID:
    """An Anthropic version inserted directly (the API would refuse it while the provider
    is disabled). Nothing here ever runs it against the real API."""
    async with sm() as s:
        agent = Agent(name=f"real-{uuid.uuid4().hex[:8]}")
        s.add(agent)
        await s.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            model_provider="anthropic",
            model=model,
            system_prompt="",
            tools=["read_file"],
            shell_allowlist=[],
            limits={"max_steps": 12, "max_cost_usd": "0.25"},
            model_settings={},
            config_hash=uuid.uuid4().hex * 2,
        )
        s.add(version)
        await s.commit()
        return version.id


@pytest.fixture
async def anthropic_client(settings: Settings, api_key: str) -> AsyncIterator[httpx.AsyncClient]:
    """The regular API with the Anthropic provider enabled (no key: nothing calls it)."""
    app = create_app(settings.model_copy(update={"enable_anthropic_provider": True}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=bearer(api_key)
        ) as c:
            yield c


@pytest.fixture
async def demo(settings: Settings) -> AsyncIterator[FastAPI]:
    # Anthropic enabled on purpose: the demo must refuse real models even then.
    app = demo_app(settings, enable_anthropic_provider=True)
    async with app.router.lifespan_context(app):
        yield app


# -- GET /providers ----------------------------------------------------------------------------


async def test_providers_report_what_this_server_runs(
    client: httpx.AsyncClient, anthropic_client: httpx.AsyncClient
) -> None:
    body = (await client.get(f"{API}/providers")).json()
    assert body["pricing_version"] == "2026-09-28"
    assert (body["real_provider_concurrency"], body["default_real_budget_usd"]) == (1, "2.00")
    by_name = {p["name"]: p for p in body["providers"]}
    assert set(by_name) == {"fake", "anthropic"}
    fake, anthropic = by_name["fake"], by_name["anthropic"]
    assert (fake["simulated"], fake["available"], fake["reason"]) == (True, True, None)
    assert fake["models"] == [
        {"id": "fake-model", "priced": True, "input_usd_per_mtok": "1",
         "output_usd_per_mtok": "5"},
    ]  # fmt: skip
    assert (anthropic["simulated"], anthropic["available"]) == (False, False)
    assert anthropic["reason"] == "Not enabled on this server (AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER)"
    assert all(m["priced"] for m in anthropic["models"])

    enabled = (await anthropic_client.get(f"{API}/providers")).json()
    real = next(p for p in enabled["providers"] if p["name"] == "anthropic")
    assert (real["available"], real["reason"]) == (True, None)
    assert "key" not in str(enabled).lower()  # no secret, nor even its name


async def test_a_demo_visitor_reads_providers_and_estimates_without_a_key(
    demo: FastAPI, client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    dataset_id, _ = await frozen_dataset(client)
    fake, real = await agent_version(sessionmaker), await real_version(sessionmaker)
    async with client_for(demo) as visitor:
        providers = (await visitor.get(f"{API}/providers")).json()["providers"]
        anthropic = next(p for p in providers if p["name"] == "anthropic")
        assert (anthropic["available"], anthropic["reason"]) == (
            False,
            "The public demo only runs simulated agents",
        )
        for version, available in ((fake, True), (real, False)):
            resp = await visitor.post(
                f"{API}/evaluations/estimate",
                json={"agent_version_id": str(version), "dataset_id": dataset_id},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["available"] is available


# -- POST /evaluations/estimate ---------------------------------------------------------------


async def test_estimate_is_an_upper_bound_from_the_version_limits(
    client: httpx.AsyncClient, sessionmaker: SM, settings: Settings
) -> None:
    dataset_id, _ = await frozen_dataset(client)  # 3 tasks
    fake = await agent_version(sessionmaker)  # default limits: 30 steps, 1.00 USD
    resp = await client.post(
        f"{API}/evaluations/estimate",
        json={"agent_version_id": str(fake), "dataset_id": dataset_id, "repetitions": 2},
    )
    assert resp.status_code == 200, resp.text
    attempts = settings.run_max_attempts
    assert resp.json() == {
        "tasks": 3, "repetitions": 2, "runs": 6, "provider": "fake", "model": "fake-model",
        "simulated": True, "available": True, "max_steps_per_run": 30,
        "run_max_attempts": attempts, "max_model_requests": 6 * 30 * attempts,
        "pricing_known": True, "pricing_version": "2026-09-28", "max_cost_usd_per_run": "1.00",
        "max_cost_usd": str(Decimal("6.00") * attempts), "default_budget_usd": None,
        "sequential": False,
    }  # fmt: skip

    real = await real_version(sessionmaker)
    body = (
        await client.post(
            f"{API}/evaluations/estimate",
            json={
                "agent_version_id": str(real), "dataset_id": dataset_id, "repetitions": 1,
                "run_max_attempts": 1,
            },
        )
    ).json()  # fmt: skip
    assert (body["provider"], body["simulated"], body["available"]) == ("anthropic", False, False)
    assert (body["max_model_requests"], body["max_cost_usd"]) == (36, "0.75")  # 3 x 12, 3 x 0.25
    assert (body["default_budget_usd"], body["sequential"]) == ("2.00", True)


async def test_estimate_errors_match_create(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    dataset_id, _ = await frozen_dataset(client)
    version = str(await agent_version(sessionmaker))
    draft = (await client.post(f"{API}/datasets", json={"name": "draft", "version": "1"})).json()
    cases: list[tuple[dict[str, Any], int]] = [
        ({"agent_version_id": str(uuid.uuid4()), "dataset_id": dataset_id}, 404),
        ({"agent_version_id": version, "dataset_id": str(uuid.uuid4())}, 404),
        ({"agent_version_id": version, "dataset_id": draft["id"]}, 409),
        ({"agent_version_id": version, "dataset_id": dataset_id, "repetitions": 11}, 422),
        ({"agent_version_id": version, "dataset_id": dataset_id, "extra": 1}, 422),
    ]
    for body, code in cases:
        assert (await client.post(f"{API}/evaluations/estimate", json=body)).status_code == code
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM evaluations"))).scalar_one() == 0


# -- POST /evaluations: budget, provider checks, context ---------------------------------------


async def test_create_stores_the_budget_and_returns_context(
    client: httpx.AsyncClient, anthropic_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    dataset_id, _ = await frozen_dataset(client)
    fake = await agent_version(sessionmaker)

    plain = await create_evaluation(client, dataset_id, fake, repetitions=1)
    assert plain.status_code == 202, plain.text
    body = plain.json()
    assert body["max_cost_usd"] is None  # a fake-model evaluation names no cap: none
    context = body["context"]
    assert context.pop("agent_id")
    assert context == {
        "agent_name": context["agent_name"], "agent_version": 1,
        "agent_config_hash": context["agent_config_hash"], "provider": "fake",
        "model": "fake-model", "simulated": True, "dataset_name": context["dataset_name"],
        "dataset_version": "1", "task_count": 3, "evaluator_version": "phase4.1",
        "pricing_version": "2026-09-28",
    }  # fmt: skip
    assert context["dataset_name"].startswith("bench-")

    capped_body = {
        "name": "capped", "dataset_id": dataset_id, "agent_version_id": str(fake),
        "max_cost_usd": "0.5",
    }  # fmt: skip
    capped = await client.post(
        f"{API}/evaluations", json=capped_body, headers={"Idempotency-Key": "cap-1"}
    )
    assert capped.status_code == 202 and capped.json()["max_cost_usd"] == "0.500000"
    same = await client.post(
        f"{API}/evaluations", json=capped_body, headers={"Idempotency-Key": "cap-1"}
    )
    assert same.status_code == 200 and same.json()["id"] == capped.json()["id"]
    changed = await client.post(
        f"{API}/evaluations",
        json={**capped_body, "max_cost_usd": "0.6"},
        headers={"Idempotency-Key": "cap-1"},
    )
    assert changed.status_code == 409
    for bad in ("0", "-1", "1000.01", "0.0000001"):
        resp = await client.post(f"{API}/evaluations", json={**capped_body, "max_cost_usd": bad})
        assert resp.status_code == 422, bad

    # A real model always gets a cap: the server default when the request names none.
    real = await real_version(sessionmaker)
    refused = await create_evaluation(client, dataset_id, real, repetitions=1)
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Provider 'anthropic' is not enabled on this server"
    created = await create_evaluation(anthropic_client, dataset_id, real, repetitions=1)
    assert created.status_code == 202, created.text
    assert created.json()["max_cost_usd"] == "2.000000"
    assert created.json()["context"]["simulated"] is False

    listed = (await client.get(f"{API}/evaluations")).json()["evaluations"]
    assert len(listed) == 3 and all(e["context"] is not None for e in listed)
    assert {e["context"]["provider"] for e in listed} == {"fake", "anthropic"}


async def test_runs_of_a_disabled_provider_are_refused(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    _, tasks = await frozen_dataset(client)
    real = await real_version(sessionmaker)
    resp = await client.post(
        f"{API}/runs", json={"task_id": tasks["A"], "agent_version_id": str(real)}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Provider 'anthropic' is not enabled on this server"


async def test_the_demo_refuses_real_models_with_403(
    demo: FastAPI, client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    dataset_id, tasks = await frozen_dataset(client)
    real = await real_version(sessionmaker)
    detail = "The public demo only runs simulated (fake-model) agents"
    async with client_for(demo) as visitor:
        evaluation = await create_evaluation(visitor, dataset_id, real, repetitions=1)
        run = await visitor.post(
            f"{API}/runs", json={"task_id": tasks["A"], "agent_version_id": str(real)}
        )
        for resp in (evaluation, run):
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == detail
        # A fake-model version still launches.
        fake = await agent_version(sessionmaker)
        ok = await create_evaluation(visitor, dataset_id, fake, repetitions=1)
        assert ok.status_code == 202, ok.text
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM evaluations"))).scalar_one() == 1


# -- compare -----------------------------------------------------------------------------------


async def test_compare_reports_context_differences_and_warnings(
    client: httpx.AsyncClient, anthropic_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)  # 3 graded, incomplete (C2, C3)
    other = (await create_evaluation(client, s.dataset_id, s.version_id, repetitions=1)).json()
    real = await real_version(sessionmaker)
    third = (await create_evaluation(anthropic_client, s.dataset_id, real, repetitions=1)).json()
    ids = f"{s.evaluation_id},{other['id']},{third['id']}"
    body = (await client.get(f"{API}/evaluations/compare", params={"ids": ids})).json()

    first, second, real_entry = body["evaluations"]
    assert first["distinguishable_from_first"] is None
    assert second["distinguishable_from_first"] is None  # nothing graded yet: no interval
    assert first["context"]["provider"] == "fake" and real_entry["context"]["simulated"] is False
    assert second["summary"]["simulated"] and not real_entry["summary"]["simulated"]
    fields = {d["field"]: d["values"] for d in body["differences"]}
    assert set(fields) == {"provider", "model", "agent_config_hash", "agent_name", "repetitions"}
    assert fields["provider"] == {
        s.evaluation_id: "fake", other["id"]: "fake", third["id"]: "anthropic",
    }  # fmt: skip
    assert fields["repetitions"][s.evaluation_id] == 3
    warnings = body["warnings"]
    assert warnings[0].startswith("Mixes simulated (fake-model) and real-model evaluations")
    assert "bench-run has only 3 graded attempts; treat differences as indicative" in warnings
    assert "bench-run is not complete; its numbers can still change" in warnings

    same = (
        await client.get(
            f"{API}/evaluations/compare", params={"ids": f"{s.evaluation_id},{other['id']}"}
        )
    ).json()
    assert {d["field"] for d in same["differences"]} == {"repetitions"}


# -- GET /stats/activity -----------------------------------------------------------------------


async def test_activity_buckets_and_evaluations(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    empty = (await client.get(f"{API}/stats/activity")).json()
    assert (empty["window_h"], empty["bucket_s"], empty["evaluations"]) == (24, 3600, [])
    assert len(empty["buckets"]) in (24, 25)  # aligned hours from now - 24 h to now
    assert all(b["finished"] == 0 and b["p95_ms"] is None for b in empty["buckets"])

    s = await seed(client, sessionmaker)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runs SET started_at = runs.finished_at - a.duration_ms * interval '1 ms' "
                "FROM run_attempts a WHERE a.run_id = runs.id"
            )
        )
        # One run finished two hours ago; and a cancelled run counts by its finished_at.
        await conn.execute(
            text(
                "UPDATE runs SET finished_at = finished_at - interval '2 hours',"
                " started_at = started_at - interval '2 hours' WHERE id = :id"
            ),
            {"id": s.runs["A2"]},
        )
    await client.post(f"{API}/runs/{s.runs['C3']}/cancel")

    body = (await client.get(f"{API}/stats/activity")).json()
    starts = [datetime.fromisoformat(b["start"]) for b in body["buckets"]]
    assert starts == sorted(starts) and all(t.minute == 0 and t.second == 0 for t in starts)
    assert all((b - a) == timedelta(hours=1) for a, b in zip(starts, starts[1:], strict=False))
    total = {k: sum(b[k] for b in body["buckets"]) for k in ("finished", "completed", "failed")}
    assert total == {"finished": 8, "completed": 6, "failed": 2}
    assert sum(b["graded"] for b in body["buckets"]) == 3
    assert sum(b["passed"] for b in body["buckets"]) == 1
    assert sum(b["cancelled"] for b in body["buckets"]) == 1
    a2_finished = SEEDED_AT - timedelta(hours=2)
    [old] = [b for b in body["buckets"] if hour_of(b["start"]) == hour_of(a2_finished)]
    assert (old["finished"], old["graded"], old["passed"]) == (1, 1, 0)
    assert old["p95_ms"] == pytest.approx(200, abs=1)  # A2 took 200 ms

    [ev] = body["evaluations"]
    assert ev["id"] == s.evaluation_id and ev["name"] == "bench-run"
    assert (ev["provider"], ev["model"], ev["simulated"], ev["agent_version"]) == (
        "fake", "fake-model", True, 1,
    )  # fmt: skip
    assert (ev["runs_total"], ev["graded"], ev["passed"]) == (9, 3, 1)
    assert ev["success_rate"] == pytest.approx(1 / 3)
    assert ev["p95_ms"] == 700  # nearest rank over the 7 finished attempts (100..700 ms)

    week = (await client.get(f"{API}/stats/activity", params={"window_h": 168})).json()
    month = (await client.get(f"{API}/stats/activity", params={"window_h": 720})).json()
    assert (week["bucket_s"], month["bucket_s"]) == (21600, 86400)
    assert len(week["buckets"]) in (28, 29) and len(month["buckets"]) in (30, 31)
    for bad in (0, 721, "x"):
        resp = await client.get(f"{API}/stats/activity", params={"window_h": bad})
        assert resp.status_code == 422


async def test_activity_lists_only_evaluations_created_in_the_window(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    dataset_id, _ = await frozen_dataset(client)
    version = await agent_version(sessionmaker)
    old = (await create_evaluation(client, dataset_id, version, repetitions=1)).json()["id"]
    new = (await create_evaluation(client, dataset_id, version, repetitions=1)).json()["id"]
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE evaluations SET created_at = now() - interval '3 days' WHERE id = :id"),
            {"id": old},
        )
    day = (await client.get(f"{API}/stats/activity")).json()["evaluations"]
    assert [e["id"] for e in day] == [new]
    assert (day[0]["runs_total"], day[0]["graded"], day[0]["success_rate"]) == (3, 0, None)
    week = (await client.get(f"{API}/stats/activity", params={"window_h": 168})).json()
    assert [e["id"] for e in week["evaluations"]] == [new, old]


# -- the worker's budget check -----------------------------------------------------------------


async def test_a_run_of_an_evaluation_past_its_budget_never_calls_the_model(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore, tmp_path: Path
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, scoring_config=DEFAULT_SCORING_CONFIG)
    async with sessionmaker() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None and run.evaluation_id is not None
        await s.execute(
            update(Evaluation)
            .where(Evaluation.id == run.evaluation_id)
            .values(max_cost_usd=Decimal("0.010000"))
        )
        # A sibling run of the same evaluation already spent the whole budget.
        sibling = Run(
            task_id=run.task_id,
            agent_version_id=run.agent_version_id,
            evaluation_id=run.evaluation_id,
            repetition=2,
            status="COMPLETED",
            outcome="AGENT_FINISHED",
            attempt_count=1,
            max_attempts=1,
            finished_at=datetime.now(UTC),
        )
        s.add(sibling)
        await s.flush()
        s.add(
            RunAttempt(
                run_id=sibling.id,
                attempt_no=1,
                worker_id="w-seed",
                status="FINISHED",
                outcome="AGENT_FINISHED",
                duration_ms=10,
                cost_usd=Decimal("0.010000"),
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        await s.commit()

    fake = RecordingFake()
    local = LocalSandbox(tmp_path / "sandbox", path=PATH)
    executor = build(sessionmaker, queue, artifact_store, local)
    executor._agent_runner._providers = {"fake": fake}  # type: ignore[union-attr]
    await handle_next(executor, queue)

    assert fake.sessions == []  # the model was never called
    async with sessionmaker() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None
        [attempt] = await RunRepository(s).list_attempts(run_id)
        assert (run.status, run.outcome, run.dead_letter_reason) == (
            "FAILED",
            "INTERNAL_ERROR",
            "non_retryable",
        )
        assert (attempt.failure_reason, attempt.retryable, attempt.steps) == (
            "evaluation_budget_exhausted",
            False,
            None,
        )
        assert run.attempt_count == 1  # not retried, although max_attempts allows more
        evaluation_id = run.evaluation_id
        category = (
            await s.execute(
                text(
                    "SELECT e.failure_category FROM attempt_evaluations e WHERE e.attempt_id = :a"
                ),
                {"a": attempt.id},
            )
        ).scalar_one_or_none()
    assert category in (None, "platform_internal")  # never counted against the agent
    assert evaluation_id is not None
    async with sessionmaker() as s:
        cap = (
            await s.execute(select(Evaluation.max_cost_usd).where(Evaluation.id == evaluation_id))
        ).scalar_one()
    assert cap == Decimal("0.010000")


async def test_a_run_under_its_budget_runs_normally(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore, tmp_path: Path
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, scoring_config=DEFAULT_SCORING_CONFIG)
    async with sessionmaker() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None
        await s.execute(
            update(Evaluation)
            .where(Evaluation.id == run.evaluation_id)
            .values(max_cost_usd=Decimal("1.000000"))
        )
        await s.commit()
    local = LocalSandbox(tmp_path / "sandbox", path=PATH)
    await handle_next(build(sessionmaker, queue, artifact_store, local), queue)
    async with sessionmaker() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None and (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
