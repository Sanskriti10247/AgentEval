"""Worker metrics (ADR-010 A) moved by the real code paths: the executor with a
real sandbox and the scripted model, the reconciler against Postgres/Redis, and
the evaluation runner. Counters are process-global, so every check is a delta."""

import re
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.recovery import RecoveryRepository
from agenteval_worker.reconciler import Reconciler
from tests.integration.test_agent_executor import (
    FIX_THE_BUG,
    executor,
    handle_next,
    load,
    make_agent_run,
    turn,
)
from tests.integration.test_evaluation_reliability import runner_for, seed_attempt
from tests.integration.test_reconciler import (
    claimed_run,
    expired_running,
    retrying_run,
)

SM = async_sessionmaker[AsyncSession]
FAKE = {"provider": "fake", "model": "fake-model"}


def val(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Snap:
    """Reads a set of series now, and reports how far each moved later."""

    def __init__(self, *series: tuple[str, dict[str, str]]) -> None:
        self.series = series
        self.before = [val(n, **lab) for n, lab in series]

    def delta(self) -> list[float]:
        return [val(n, **lab) - b for (n, lab), b in zip(self.series, self.before, strict=True)]


@pytest.mark.docker
async def test_a_real_agent_run_moves_run_model_and_tool_metrics(
    sessionmaker: SM, queue: RunQueue
) -> None:
    snap = Snap(
        ("agenteval_runs_finished_total", {"status": "COMPLETED", "outcome": "AGENT_FINISHED"}),
        ("agenteval_run_transitions_total", {"code": "R1", "actor": "worker"}),
        ("agenteval_run_transitions_total", {"code": "R3", "actor": "worker"}),
        ("agenteval_run_transitions_total", {"code": "R7", "actor": "worker"}),
        ("agenteval_run_queue_wait_seconds_count", {}),
        ("agenteval_run_duration_seconds_count", {"outcome": "AGENT_FINISHED"}),
        ("agenteval_model_tokens_total", {**FAKE, "direction": "input"}),
        ("agenteval_model_tokens_total", {**FAKE, "direction": "output"}),
        ("agenteval_model_call_duration_seconds_count", FAKE),
        ("agenteval_model_cost_usd_total", FAKE),
        ("agenteval_tool_calls_total", {"tool": "run_tests", "status": "ok"}),
        ("agenteval_tool_calls_total", {"tool": "write_file", "status": "ok"}),
        ("agenteval_tool_call_duration_seconds_count", {"tool": "run_tests"}),
    )
    run_id = await make_agent_run(sessionmaker, queue, FIX_THE_BUG)
    await handle_next(executor(sessionmaker, queue), queue)
    run, [attempt], _ = await load(sessionmaker, run_id)
    assert run.status == "COMPLETED"

    d = snap.delta()
    assert d[:6] == [1, 1, 1, 1, 1, 1]
    assert d[6:9] == [400, 80, 4]  # four scripted calls at the fake's 100 in / 20 out
    assert d[9] == pytest.approx(float(attempt.cost_usd or 0))
    assert d[10:] == [2, 1, 2]
    assert val("agenteval_worker_inflight", kind="run") == 0  # back down after the run


@pytest.mark.docker
@pytest.mark.parametrize(
    ("max_attempts", "code", "dead_letter"),
    [(2, "R8", None), (1, "R10", "retries_exhausted")],
)
async def test_a_retryable_model_failure_counts_a_retry_or_a_dead_letter(
    sessionmaker: SM, queue: RunQueue, max_attempts: int, code: str, dead_letter: str | None
) -> None:
    snap = Snap(
        ("agenteval_run_transitions_total", {"code": code, "actor": "worker"}),
        (
            "agenteval_attempt_failures_total",
            {"failure_reason": "model_overloaded", "category": "model_transient"},
        ),
        ("agenteval_runs_finished_total", {"status": "FAILED", "outcome": "MODEL_ERROR"}),
        ("agenteval_run_dead_letters_total", {"reason": "retries_exhausted"}),
        ("agenteval_model_call_errors_total", {**FAKE, "kind": "overloaded"}),
    )
    script: list[dict[str, Any]] = [{"type": "error", "kind": "overloaded"} for _ in range(4)]
    run_id = await make_agent_run(sessionmaker, queue, script, max_attempts=max_attempts)
    await handle_next(executor(sessionmaker, queue), queue)
    run, _, _ = await load(sessionmaker, run_id)
    assert run.status == ("RETRYING" if dead_letter is None else "FAILED")

    retried = dead_letter is None
    assert snap.delta() == [1, 1, 0 if retried else 1, 0 if retried else 1, 4]


@pytest.mark.docker
async def test_a_refused_model_is_a_completed_run_not_an_attempt_failure(
    sessionmaker: SM, queue: RunQueue
) -> None:
    snap = Snap(
        ("agenteval_runs_finished_total", {"status": "COMPLETED", "outcome": "MODEL_REFUSAL"}),
        (
            "agenteval_attempt_failures_total",
            {"failure_reason": "model_overloaded", "category": "model_transient"},
        ),
    )
    await make_agent_run(sessionmaker, queue, [turn(stop="refusal", stop_detail="cyber")])
    await handle_next(executor(sessionmaker, queue), queue)
    assert snap.delta() == [1, 0]


def build_reconciler(sm: SM, queue: RunQueue) -> Reconciler:
    return Reconciler(
        sm,
        queue,
        "w-metrics",
        interval_s=0.05,
        lease_ttl_s=0.2,
        redispatch_after_s=60.0,
        orphan_sweep_interval_s=300.0,
        kill_attempt=lambda _a: 1,
        sweep_orphans=lambda: 0,
        clock=lambda: 0.0,
    )


async def test_reconciler_recoveries_are_counted_by_transition_code(
    engine: AsyncEngine, sessionmaker: SM, queue: RunQueue
) -> None:
    rec = build_reconciler(sessionmaker, queue)
    snap = Snap(
        ("agenteval_run_transitions_total", {"code": "R4a", "actor": "reconciler"}),
        ("agenteval_run_transitions_total", {"code": "R9", "actor": "reconciler"}),
        ("agenteval_run_transitions_total", {"code": "R10", "actor": "reconciler"}),
        ("agenteval_run_transitions_total", {"code": "R11", "actor": "reconciler"}),
        ("agenteval_run_transitions_total", {"code": "R12", "actor": "reconciler"}),
        ("agenteval_run_dead_letters_total", {"reason": "retries_exhausted"}),
        ("agenteval_run_cancellations_total", {"reason": "batch"}),
        ("agenteval_runs_finished_total", {"status": "FAILED", "outcome": "INTERNAL_ERROR"}),
        (
            "agenteval_attempt_failures_total",
            {"failure_reason": "lease_expired", "category": "lease"},
        ),
    )
    await claimed_run(engine)  # R4a
    await expired_running(engine, max_attempts=3)  # R9
    await expired_running(engine, max_attempts=1)  # R10
    await expired_running(engine, cancel="batch")  # R11
    await retrying_run(engine, due=True)  # R12

    await rec.tick()
    # R9, R10 and R11 each abandoned an attempt (lease_expired).
    assert snap.delta() == [1, 1, 1, 1, 1, 1, 1, 1, 3]


async def test_a_failing_reconciler_step_is_counted_by_step_name(
    sessionmaker: SM, queue: RunQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = Snap(("agenteval_reconciler_step_errors_total", {"step": "recover_claimed"}))

    async def broken(self: RecoveryRepository, *a: Any, **k: Any) -> Any:
        raise RuntimeError("step 1a is broken")

    monkeypatch.setattr(RecoveryRepository, "recover_expired_claims", broken)
    await build_reconciler(sessionmaker, queue).tick()
    assert snap.delta() == [1]


@pytest.mark.docker
async def test_evaluation_metrics_follow_the_grading_transitions(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    snap = Snap(
        ("agenteval_evaluation_transitions_total", {"code": "E1"}),
        ("agenteval_evaluation_transitions_total", {"code": "E2"}),
        ("agenteval_evaluation_transitions_total", {"code": "E4"}),
        ("agenteval_evaluation_transitions_total", {"code": "E5"}),
        ("agenteval_evaluation_duration_seconds_count", {"status": "SUCCEEDED"}),
    )
    graded = await seed_attempt(sessionmaker)
    rec = await runner.record_attempt(graded.attempt_id)
    assert rec is not None
    assert await runner.grade(rec.evaluation_id) == rec.evaluation_id
    skipped = await seed_attempt(sessionmaker, spec=None)
    await runner.record_attempt(skipped.attempt_id)

    assert snap.delta() == [1, 1, 1, 1, 1]


def test_after_real_runs_no_label_value_looks_like_an_id() -> None:
    """The cardinality rule, checked against what the real code paths above (and
    every other test in the session) actually emitted."""
    uuid_like = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    offenders = [
        (sample.name, key, value)
        for family in REGISTRY.collect()
        if family.name.startswith("agenteval_")
        for sample in family.samples
        for key, value in sample.labels.items()
        if uuid_like.search(value)
    ]
    assert offenders == []
