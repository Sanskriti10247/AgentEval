"""Phase 5 Step 7 (PROGRESS.md section 19): evaluation reliability against real
Postgres and Redis. Every evaluation transition (E1-E9b), the pinned grading
budget and G9, fencing of a stale grader, lease loss, A1 recovery, dispatch and
consumption on the evaluations stream, and a SIGKILLed grader recovered by
another worker.

Most tests need no Docker: attempts are seeded as rows and graded with a
final-answer-only spec (A6: no container), or with a workspace spec whose
EvaluationSession.open is replaced, so each failure mode is injected
deterministically. The lease-loss-kills-the-container and the crash tests use
a real agent run and a real evaluation container.
"""

import asyncio
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import docker
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.config import Settings
from agenteval_core.evaluation.scoring import DEFAULT_SCORING_CONFIG
from agenteval_core.models import (
    Agent,
    AgentVersion,
    AttemptArtifact,
    AttemptEvaluation,
    Dataset,
    Evaluation,
    Run,
    RunAttempt,
    Score,
    Task,
)
from agenteval_core.queue.streams import EVALUATION_FIELD, RunQueue, read_any
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_core.repositories.evaluations import EvaluationRepository
from agenteval_worker.consumer import Consumer
from agenteval_worker.evaluation_runner import EvaluationRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.reconciler import Reconciler
from agenteval_worker.sandbox import evaluation as eval_sandbox
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox
from agenteval_worker.sandbox.session import SandboxError
from tests.integration.conftest import SANDBOX_IMAGE
from tests.integration.phase5_seed import running_run
from tests.integration.test_agent_executor import handle_next, leftover_containers
from tests.integration.test_evaluation_runner import GIVE_UP, HIDDEN_SPEC, build, make_run
from tests.integration.test_leases import wait_until

SM = async_sessionmaker[AsyncSession]
FINAL_SPEC: dict[str, Any] = {
    "checks": [{"kind": "final_answer", "name": "a", "match": "contains", "expected": "sign"}]
}
SLOW_SPEC: dict[str, Any] = {
    "checks": [{"kind": "io", "name": "slow", "argv": ["python", "-c",
                "import time; time.sleep(45)"], "expected_stdout": "", "timeout_s": 60}],
    "timeout_s": 60,
}  # fmt: skip
HARNESS = os.path.join(os.path.dirname(__file__), "_harness", "eval_worker_harness.py")


# -- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
async def eval_queue(redis: Redis, settings: Settings) -> RunQueue:
    q = RunQueue(
        redis,
        settings.evaluation_stream,
        settings.consumer_group,
        maxlen=settings.stream_maxlen,
        field=EVALUATION_FIELD,
    )
    await q.ensure_group()
    return q


class FakeLeases:
    """Stands in for the LeaseManager: records holds and releases, and lets a
    test fire the lease-lost callback itself."""

    def __init__(self) -> None:
        self.held: dict[tuple[str, str], tuple[int, Callable[[], None]]] = {}
        self.released: list[tuple[str, str, int]] = []

    def hold(
        self, kind: str, id_: str, epoch: int, t_sent: float, *, on_lease_lost: Any, **_: Any
    ) -> None:
        self.held[(kind, id_)] = (epoch, on_lease_lost)

    def release(self, kind: str, id_: str, epoch: int) -> None:
        self.released.append((kind, id_, epoch))
        self.held.pop((kind, id_), None)


def runner_for(
    sm: SM,
    store: LocalArtifactStore,
    *,
    budget: int = 3,
    leases: Any = None,
    kills: list[str] | None = None,
    **kw: Any,
) -> EvaluationRunner:
    return EvaluationRunner(
        sessionmaker=sm,
        docker_client=docker.from_env(),
        artifact_store=store,
        worker_id="w-test",
        eval_max_attempts=budget,
        lease_manager=leases,
        kill_by_attempt_label=(lambda a: kills.append(a) or 1) if kills is not None else None,
        **kw,
    )


@dataclass
class Seeded:
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    batch_id: uuid.UUID | None


async def seed_attempt(
    sm: SM,
    *,
    spec: dict[str, Any] | None = FINAL_SPEC,
    outcome: str = "AGENT_FINISHED",
    failure_reason: str | None = None,
    capture: str | None = None,
    batch_budget: int | None = None,
    scoring_config: dict[str, Any] | None = None,
    finished_ago_s: float = 0.0,
    final_answer: str = "a sign",
) -> Seeded:
    """A FINISHED agent attempt as the executor leaves it (the run's terminal
    state included), optionally part of a batch with its own eval budget."""
    finished = datetime.now(UTC) - timedelta(seconds=finished_ago_s)
    async with sm() as s:
        agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}")
        s.add(agent)
        await s.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            model_provider="fake",
            model="fake-model",
            system_prompt="",
            tools=["read_file"],
            shell_allowlist=[],
            limits={},
            model_settings={},
            config_hash=uuid.uuid4().hex * 2,
        )
        task = Task(
            name="seeded",
            task_type="agent",
            image=SANDBOX_IMAGE,
            instructions="Fix calc.py.",
            files={"calc.py": "x = 1\n", "test_calc.py": "pass\n"},
            timeout_s=120,
            memory_mb=256,
            cpu_limit=1.0,
            evaluation=spec,
        )
        s.add_all([version, task])
        await s.flush()
        batch_id = None
        if batch_budget is not None:
            dataset = Dataset(name=f"d-{uuid.uuid4().hex[:6]}", version="1", status="DRAFT")
            s.add(dataset)
            await s.flush()
            config = scoring_config or DEFAULT_SCORING_CONFIG.model_dump(mode="json")
            batch = Evaluation(
                name="batch",
                dataset_id=dataset.id,
                agent_version_id=version.id,
                repetitions=1,
                scoring_config=config,
                scoring_config_hash=DEFAULT_SCORING_CONFIG.config_hash(),
                eval_max_attempts=batch_budget,
            )
            s.add(batch)
            await s.flush()
            batch_id = batch.id
        failed = outcome != "AGENT_FINISHED"
        run = Run(
            task_id=task.id,
            agent_version_id=version.id,
            evaluation_id=batch_id,
            repetition=1 if batch_id else None,
            status="FAILED" if failed else "COMPLETED",
            outcome=outcome,
            worker_id="w-seed",
            attempt_count=1,
            error_message="seeded failure" if failed else None,
            dead_letter_reason="non_retryable" if failed else None,
            finished_at=finished,
        )
        s.add(run)
        await s.flush()
        attempt = RunAttempt(
            run_id=run.id,
            attempt_no=1,
            worker_id="w-seed",
            status="FINISHED",
            outcome=outcome,
            duration_ms=100,
            steps=4,
            tokens_in=100,
            tokens_out=20,
            cost_usd=Decimal("0.010000"),
            final_answer=final_answer,
            error_message="seeded failure" if failed else None,
            started_at=finished,
            finished_at=finished,
            failure_reason=failure_reason,
            retryable=None if failure_reason is None else False,
            workspace_capture=capture,
        )
        s.add(attempt)
        await s.commit()
        return Seeded(attempt.id, run.id, task.id, batch_id)


async def add_snapshot(sm: SM, store: LocalArtifactStore, attempt_id: uuid.UUID) -> None:
    """An artifact row backed by a real file: enough for `_load_workspace`
    (the bytes are never unpacked when EvaluationSession.open is replaced)."""
    stored = await store.put(attempt_id, "workspace", b"not-really-a-tarball")
    async with sm() as s:
        s.add(
            AttemptArtifact(
                attempt_id=attempt_id,
                kind="workspace",
                size_bytes=stored.size,
                sha256=stored.sha256,
                storage_key=stored.key,
            )
        )
        await s.commit()


async def workspace_attempt(sm: SM, store: LocalArtifactStore, **kw: Any) -> Seeded:
    seeded = await seed_attempt(sm, spec=HIDDEN_SPEC, capture="stored", **kw)
    await add_snapshot(sm, store, seeded.attempt_id)
    return seeded


async def evaluation_of(sm: SM, attempt_id: uuid.UUID) -> AttemptEvaluation:
    async with sm() as s:
        row = (
            await s.execute(
                select(AttemptEvaluation)
                .where(AttemptEvaluation.attempt_id == attempt_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return row


async def scores_of(sm: SM, evaluation_id: uuid.UUID) -> list[Score]:
    async with sm() as s:
        return list(
            (
                await s.execute(select(Score).where(Score.attempt_evaluation_id == evaluation_id))
            ).scalars()
        )


async def recorded(runner: EvaluationRunner, attempt_id: uuid.UUID) -> uuid.UUID:
    rec = await runner.record_attempt(attempt_id)
    assert rec is not None and rec.status == "PENDING"
    return rec.evaluation_id


async def expire_lease(sm: SM, evaluation_id: uuid.UUID) -> None:
    async with sm() as s:
        await s.execute(
            update(AttemptEvaluation)
            .where(AttemptEvaluation.id == evaluation_id)
            .values(lease_expires_at=text("now() - interval '1 second'"))
        )
        await s.commit()


async def make_due(sm: SM, evaluation_id: uuid.UUID) -> None:
    async with sm() as s:
        await s.execute(
            update(AttemptEvaluation)
            .where(AttemptEvaluation.id == evaluation_id)
            .values(next_attempt_at=text("now() - interval '1 second'"))
        )
        await s.commit()


async def assert_g9(sm: SM) -> None:
    """G9: no PENDING row is ever exhausted (the CHECK enforces it; this
    proves the path under test left none behind)."""
    async with sm() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM attempt_evaluations"
                    " WHERE status = 'PENDING' AND eval_attempts >= max_eval_attempts"
                )
            )
        ).scalar_one()
    assert n == 0


def failing_open(error: Exception, *, delay_s: float = 0.0) -> Any:
    def broken(*a: Any, **kw: Any) -> Any:
        if delay_s:
            time.sleep(delay_s)
        raise error

    return broken


def build_reconciler(
    sm: SM,
    queue: RunQueue,
    eval_queue: RunQueue,
    runner: EvaluationRunner | None,
    kills: list[str],
    *,
    redispatch_after_s: float = 60.0,
    a1_grace_s: float = 60.0,
    interval_s: float = 0.05,
    ttl_s: float = 0.2,
) -> Reconciler:
    return Reconciler(
        sm,
        queue,
        "w-rec",
        interval_s=interval_s,
        lease_ttl_s=ttl_s,
        redispatch_after_s=redispatch_after_s,
        orphan_sweep_interval_s=300.0,
        kill_attempt=lambda attempt_id: kills.append(attempt_id) or 1,
        sweep_orphans=lambda: 0,
        clock=lambda: 0.0,
        evaluation_queue=eval_queue,
        record_attempt=runner.record_attempt if runner is not None else None,
        a1_grace_s=a1_grace_s,
    )


# -- E1 / E2 / E3: record, F11, the pinned budget (F9) -----------------------------------------


async def test_e1_record_pins_the_ad_hoc_budget_from_the_setting(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    seeded = await seed_attempt(sessionmaker)
    runner = runner_for(sessionmaker, artifact_store, budget=4)
    rec = await runner.record_attempt(seeded.attempt_id)
    assert rec is not None and rec.status == "PENDING"
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.eval_attempts, ev.max_eval_attempts) == ("PENDING", 0, 4)
    assert ev.lease_expires_at is None and ev.error_reason is None and ev.metrics is not None
    # A second recorder (a duplicate, or A1 recovery) stops at ON CONFLICT DO NOTHING.
    assert await runner.record_attempt(seeded.attempt_id) is None
    await assert_g9(sessionmaker)


async def test_a_batch_run_is_pinned_with_the_batchs_budget_not_the_setting(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    batch_run = await seed_attempt(sessionmaker, batch_budget=2)
    ad_hoc = await seed_attempt(sessionmaker)
    runner = runner_for(sessionmaker, artifact_store, budget=3)
    await runner.record_attempt(batch_run.attempt_id)
    await runner.record_attempt(ad_hoc.attempt_id)
    assert (await evaluation_of(sessionmaker, batch_run.attempt_id)).max_eval_attempts == 2
    assert (await evaluation_of(sessionmaker, ad_hoc.attempt_id)).max_eval_attempts == 3
    # Direct construction keeps Phase 4's single try (F9).
    plain = EvaluationRunner(
        sessionmaker=sessionmaker, docker_client=docker.from_env(), artifact_store=artifact_store
    )
    other = await seed_attempt(sessionmaker)
    await plain.record_attempt(other.attempt_id)
    assert (await evaluation_of(sessionmaker, other.attempt_id)).max_eval_attempts == 1


async def test_f11_rows_exist_only_for_finished_attempts(
    sessionmaker: SM, engine: Any, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    _, running = await running_run(engine)  # a RUNNING attempt
    _, abandoned = await running_run(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR',"
                " failure_reason = 'lease_expired', retryable = true, finished_at = now()"
                " WHERE id = :a"
            ),
            {"a": abandoned},
        )
    assert await runner.record_attempt(running) is None
    assert await runner.record_attempt(abandoned) is None
    assert await runner.record_attempt(uuid.uuid4()) is None  # no such attempt
    async with sessionmaker() as s:
        assert (await s.execute(text("SELECT count(*) FROM attempt_evaluations"))).scalar_one() == 0


async def test_e2_platform_failures_and_specless_tasks_are_skipped(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    platform = await seed_attempt(
        sessionmaker, outcome="SANDBOX_ERROR", failure_reason="sandbox_lost"
    )
    specless = await seed_attempt(sessionmaker, spec=None)
    for seeded, category in ((platform, "platform_sandbox"), (specless, None)):
        rec = await runner.record_attempt(seeded.attempt_id)
        assert rec is not None and rec.status == "SKIPPED"
        ev = await evaluation_of(sessionmaker, seeded.attempt_id)
        assert (ev.status, ev.failure_category, ev.eval_attempts) == ("SKIPPED", category, 0)
        assert ev.error_reason is None and ev.lease_expires_at is None


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("spec", "spec_invalid"),
        ("scoring", "scoring_config_invalid"),
        ("too_large", "capture_too_large"),
        ("incomplete", "capture_incomplete"),
        ("invalid", "capture_invalid"),
        ("persist_failed", "capture_persist_failed"),
        ("no_row", "capture_missing"),
    ],
)
async def test_e3_creation_time_errors_are_non_retryable_with_a_reason(
    sessionmaker: SM, artifact_store: LocalArtifactStore, kind: str, reason: str
) -> None:
    if kind == "spec":
        seeded = await seed_attempt(sessionmaker, spec={"checks": "not-a-list"})
    elif kind == "scoring":
        seeded = await seed_attempt(sessionmaker, batch_budget=3, scoring_config={"v": "bogus"})
    elif kind == "no_row":
        seeded = await seed_attempt(sessionmaker, spec=HIDDEN_SPEC, capture="stored")
    else:
        seeded = await seed_attempt(sessionmaker, spec=HIDDEN_SPEC, capture=kind)
    rec = await runner_for(sessionmaker, artifact_store).record_attempt(seeded.attempt_id)
    assert rec is not None and rec.status == "ERROR"
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.error_reason, ev.error_kind) == ("ERROR", reason, "non_retryable")
    assert (ev.failure_category, ev.eval_attempts, ev.lease_expires_at) == (
        "evaluation_error",
        0,
        None,
    )
    assert ev.error_message is not None


# -- E4: claim ---------------------------------------------------------------------------------


async def test_e4_claim_takes_a_lease_and_consumes_an_attempt(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    eid = await recorded(runner, (await seed_attempt(sessionmaker)).attempt_id)

    async def claim() -> Any:
        async with sessionmaker() as s:
            got = await EvaluationRepository(s).claim(eid, "w-1", ttl_s=30)
            await s.commit()
            return got

    claims = await asyncio.gather(*(claim() for _ in range(4)))
    [won] = [c for c in claims if c is not None]  # exactly one winner
    assert (won.lease_epoch, won.eval_attempts, won.max_eval_attempts) == (1, 1, 3)
    async with sessionmaker() as s:
        ev = await s.get(AttemptEvaluation, eid, populate_existing=True)
    assert ev is not None
    assert (ev.status, ev.worker_id, ev.lease_epoch, ev.eval_attempts) == ("RUNNING", "w-1", 1, 1)
    assert ev.lease_expires_at is not None and ev.heartbeat_at is not None
    assert ev.started_at is not None and ev.error_reason is None and ev.next_attempt_at is None


async def test_e4_claim_waits_for_the_backoff_and_keeps_the_first_started_at(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    seeded = await seed_attempt(sessionmaker)
    eid = await recorded(runner, seeded.attempt_id)
    async with sessionmaker() as s:
        repo = EvaluationRepository(s)
        first = await repo.claim(eid, "w-1")
        assert first is not None
        # E7 by hand: back to PENDING with a backoff and a reason.
        assert await repo.release(eid, 1, error_reason="sandbox_failed", retry_in_s=60)
        await s.commit()
    async with sessionmaker() as s:
        started = (await s.get(AttemptEvaluation, eid, populate_existing=True)).started_at  # type: ignore[union-attr]
        assert await EvaluationRepository(s).claim(eid, "w-2") is None  # not due yet
        await s.rollback()
    await make_due(sessionmaker, eid)
    async with sessionmaker() as s:
        second = await EvaluationRepository(s).claim(eid, "w-2")
        await s.commit()
    assert second is not None and (second.lease_epoch, second.eval_attempts) == (2, 2)
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert ev.started_at == started  # the first claim's time is kept
    assert (ev.error_reason, ev.next_attempt_at, ev.worker_id) == (None, None, "w-2")  # G13


# -- E5: success -------------------------------------------------------------------------------


async def test_e5_grade_succeeds_with_a_score_and_releases_the_lease(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    leases = FakeLeases()
    runner = runner_for(sessionmaker, artifact_store, leases=leases)
    seeded = await seed_attempt(sessionmaker)
    eid = await recorded(runner, seeded.attempt_id)
    # A previous failed attempt left its reason behind; E4 clears it and E5 keeps it clear.
    async with sessionmaker() as s:
        await s.execute(
            update(AttemptEvaluation)
            .where(AttemptEvaluation.id == eid)
            .values(error_reason="sandbox_failed", eval_attempts=1, started_at=datetime.now(UTC))
        )
        await s.commit()

    assert await runner.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", True, "passed")
    assert (ev.eval_attempts, ev.lease_epoch, ev.worker_id) == (2, 1, "w-test")
    assert ev.lease_expires_at is None and ev.error_reason is None and ev.error_kind is None
    assert ev.finished_at is not None and ev.duration_ms is not None
    [score] = await scores_of(sessionmaker, eid)
    assert score.scoring_config_hash == DEFAULT_SCORING_CONFIG.config_hash()
    # The lease was held for the claim's epoch and released right after the finish.
    assert leases.released == [("evaluation", str(eid), 1)] and not leases.held


async def test_grading_loads_everything_from_the_database_for_any_worker(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    """The recording worker and the grading worker share nothing but Postgres."""
    seeded = await seed_attempt(sessionmaker, final_answer="no match here")
    eid = await recorded(runner_for(sessionmaker, artifact_store), seeded.attempt_id)
    other = runner_for(sessionmaker, artifact_store)
    assert await other.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", False, "wrong_answer")


async def test_a_duplicate_grade_loses_the_claim_and_changes_nothing(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    seeded = await seed_attempt(sessionmaker)
    eid = await recorded(runner, seeded.attempt_id)
    acks: list[int] = []

    async def ack() -> None:
        acks.append(1)

    results = await asyncio.gather(*(runner.grade(eid, acknowledge=ack) for _ in range(3)))
    assert results.count(eid) == 1 and results.count(None) == 2
    assert await runner.grade(eid, acknowledge=ack) is None
    assert len(acks) == 4  # the message is acked whether the claim is won or not
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.eval_attempts, ev.lease_epoch) == ("SUCCEEDED", 1, 1)
    assert len(await scores_of(sessionmaker, eid)) == 1


# -- E6: non-retryable errors at grade time ---------------------------------------------------


@pytest.mark.parametrize("what", ["spec", "scoring", "artifact_missing", "artifact_corrupt"])
async def test_e6_a_non_retryable_failure_is_final_despite_budget_left(
    sessionmaker: SM, artifact_store: LocalArtifactStore, what: str
) -> None:
    if what in ("artifact_missing", "artifact_corrupt"):
        seeded = await workspace_attempt(sessionmaker, artifact_store, batch_budget=3)
        key = (await artifact_store.list_files())[0][0]
        path = artifact_store._path(key)  # noqa: SLF001
        if what == "artifact_missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupted!!")
    else:
        seeded = await seed_attempt(sessionmaker, batch_budget=3)
    runner = runner_for(sessionmaker, artifact_store, budget=3)
    eid = await recorded(runner, seeded.attempt_id)
    # What record() saw was fine; it changes before grading (e.g. a config edit).
    async with sessionmaker() as s:
        if what == "spec":
            await s.execute(
                update(Task).where(Task.id == seeded.task_id).values(evaluation={"checks": 7})
            )
        elif what == "scoring":
            await s.execute(
                update(Evaluation)
                .where(Evaluation.id == seeded.batch_id)
                .values(scoring_config={"v": "bogus"})
            )
        await s.commit()

    assert await runner.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    expected = {
        "spec": "spec_invalid",
        "scoring": "scoring_config_invalid",
        "artifact_missing": "artifact_missing",
        "artifact_corrupt": "artifact_corrupt",
    }[what]
    assert (ev.status, ev.error_reason, ev.error_kind) == ("ERROR", expected, "non_retryable")
    assert (ev.eval_attempts, ev.max_eval_attempts, ev.lease_expires_at) == (1, 3, None)
    assert ev.failure_category == "evaluation_error" and ev.worker_id == "w-test"  # G13
    assert await scores_of(sessionmaker, eid) == []


# -- E7: transient failure, budget left -> PENDING with backoff; then exhaustion --------------


@pytest.mark.parametrize(
    ("error", "delay_s", "reason"),
    [
        (SandboxError("docker error: boom", reason="sandbox_create_failed"), 0.0, "sandbox_failed"),  # type: ignore[arg-type]
        (RuntimeError("bug"), 0.0, "internal_unexpected"),
        (SandboxError("slow", reason="sandbox_create_failed"), 2.0, "budget_timeout"),  # type: ignore[arg-type]
    ],
    ids=["sandbox_failed", "internal_unexpected", "budget_timeout"],
)
async def test_e7_a_transient_failure_goes_back_to_pending_with_backoff_then_e6(
    sessionmaker: SM,
    artifact_store: LocalArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    delay_s: float,
    reason: str,
) -> None:
    monkeypatch.setattr(
        eval_sandbox.EvaluationSession, "open", failing_open(error, delay_s=delay_s)
    )
    checks = [{**HIDDEN_SPEC["checks"][0], "timeout_s": 5}]
    spec = {**HIDDEN_SPEC, "checks": checks, "timeout_s": 10}  # 10 s is the smallest allowed
    seeded = await seed_attempt(sessionmaker, spec=spec, capture="stored", batch_budget=2)
    await add_snapshot(sessionmaker, artifact_store, seeded.attempt_id)
    # A negative grace leaves the container-opening step a 1 s budget (10 - 9), so the
    # slow `open` of the budget_timeout case overruns it without a ten-second test.
    runner = runner_for(sessionmaker, artifact_store, setup_grace_s=-9)
    eid = await recorded(runner, seeded.attempt_id)

    before = datetime.now(UTC)
    assert await runner.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    # E7: PENDING, the reason kept, the lease cleared, a backoff of 2 s * U(0.8, 1.2).
    assert (ev.status, ev.error_reason, ev.eval_attempts, ev.lease_epoch) == (
        "PENDING",
        reason,
        1,
        1,
    )
    assert ev.lease_expires_at is None and ev.error_kind is None and ev.finished_at is None
    assert ev.next_attempt_at is not None
    delay = (ev.next_attempt_at - before).total_seconds()
    assert 1.4 < delay < 2.8 + (delay_s or 0)  # backoff_delay(1), plus the failing attempt itself
    await assert_g9(sessionmaker)
    assert await runner.grade(eid) is None  # the backoff has not elapsed: not claimable

    await make_due(sessionmaker, eid)
    assert await runner.grade(eid) == eid  # attempt 2 of 2 fails too: E6 retries_exhausted
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.error_reason, ev.error_kind) == ("ERROR", reason, "retries_exhausted")
    assert (ev.eval_attempts, ev.max_eval_attempts, ev.lease_epoch) == (2, 2, 2)
    assert ev.lease_expires_at is None and ev.failure_category == "evaluation_error"
    await assert_g9(sessionmaker)
    assert await scores_of(sessionmaker, eid) == []


@pytest.mark.docker
async def test_a_retry_that_succeeds_clears_the_reason(
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = eval_sandbox.EvaluationSession.open.__func__  # type: ignore[attr-defined]
    calls = 0

    def flaky(cls: Any, *a: Any, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SandboxError("docker error: once", reason="sandbox_create_failed")  # type: ignore[arg-type]
        return real_open(cls, *a, **kw)

    monkeypatch.setattr(eval_sandbox.EvaluationSession, "open", classmethod(flaky))
    # A real snapshot is needed for the second attempt: use a real agent run.
    run_id = await _real_attempt(sessionmaker, queue, artifact_store, GIVE_UP, HIDDEN_SPEC)
    attempt = await _finished_attempt(sessionmaker, run_id)
    runner = runner_for(sessionmaker, artifact_store, budget=2)
    eid = await recorded(runner, attempt.id)
    assert await runner.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, attempt.id)
    assert (ev.status, ev.error_reason) == ("PENDING", "sandbox_failed")
    await make_due(sessionmaker, eid)
    assert await runner.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, attempt.id)
    assert (ev.status, ev.eval_attempts, ev.error_reason, ev.error_kind) == (
        "SUCCEEDED",
        2,
        None,
        None,
    )
    assert len(await scores_of(sessionmaker, eid)) == 1
    assert leftover_containers(run_id) == []


# -- E8a / E8b: shutdown mid-grading -----------------------------------------------------------


@pytest.mark.parametrize(("budget", "expected"), [(2, "PENDING"), (1, "ERROR")])
async def test_e8_a_hard_cancel_releases_or_exhausts(
    sessionmaker: SM,
    artifact_store: LocalArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    expected: str,
) -> None:
    gate = threading.Event()
    entered = threading.Event()

    def blocking(*a: Any, **kw: Any) -> Any:
        entered.set()
        gate.wait(10)
        raise SandboxError("late", reason="sandbox_create_failed")  # type: ignore[arg-type]

    monkeypatch.setattr(eval_sandbox.EvaluationSession, "open", blocking)
    seeded = await workspace_attempt(sessionmaker, artifact_store, batch_budget=budget)
    runner = runner_for(sessionmaker, artifact_store, budget=budget)
    eid = await recorded(runner, seeded.attempt_id)

    task = asyncio.create_task(runner.grade(eid))
    async with asyncio.timeout(10):
        while not entered.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    task.cancel()
    gate.set()  # let the container-opening thread finish so cleanup can complete
    with pytest.raises(asyncio.CancelledError):
        await task

    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert ev.status == expected and ev.error_reason == "worker_shutdown"
    assert (ev.eval_attempts, ev.lease_expires_at) == (1, None)  # the attempt is consumed
    if expected == "PENDING":  # E8a: due at once, no backoff
        assert ev.next_attempt_at is not None and ev.error_kind is None
        assert ev.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=1)
    else:  # E8b
        assert ev.error_kind == "retries_exhausted" and ev.failure_category == "evaluation_error"
        assert ev.error_message == "interrupted: worker_shutdown"
    await assert_g9(sessionmaker)


# -- E9a / E9b: the reconciler recovers expired leases ----------------------------------------


@pytest.mark.parametrize(("budget", "expected"), [(3, "PENDING"), (1, "ERROR")])
async def test_e9_expired_leases_are_recovered_and_the_container_killed(
    sessionmaker: SM,
    queue: RunQueue,
    eval_queue: RunQueue,
    artifact_store: LocalArtifactStore,
    budget: int,
    expected: str,
) -> None:
    seeded = await seed_attempt(sessionmaker, batch_budget=budget)
    runner = runner_for(sessionmaker, artifact_store, budget=budget)
    eid = await recorded(runner, seeded.attempt_id)
    async with sessionmaker() as s:
        assert await EvaluationRepository(s).claim(eid, "w-dead") is not None
        await s.commit()
    kills: list[str] = []
    rec = build_reconciler(sessionmaker, queue, eval_queue, runner, kills)

    assert await rec.step_recover_evaluations() == []  # the lease is still valid
    await expire_lease(sessionmaker, eid)
    assert await rec.step_recover_evaluations() == [seeded.attempt_id]
    assert kills == [str(seeded.attempt_id)]  # after commit, by the attempt label

    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.error_reason, ev.lease_epoch, ev.eval_attempts) == (
        expected,
        "lease_expired",
        2,
        1,
    )
    assert ev.lease_expires_at is None and ev.worker_id == "w-dead"  # G13
    if expected == "PENDING":  # E9a: a backoff, no terminal fields
        assert ev.next_attempt_at is not None and ev.finished_at is None
        assert ev.next_attempt_at > datetime.now(UTC)
    else:  # E9b
        assert (ev.error_kind, ev.failure_category) == ("retries_exhausted", "evaluation_error")
        assert ev.finished_at is not None and ev.error_message is not None
    await assert_g9(sessionmaker)
    assert await rec.step_recover_evaluations() == []  # idempotent


async def test_a_stale_grader_is_fenced_after_e9a_and_writes_no_score(
    sessionmaker: SM, queue: RunQueue, eval_queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    seeded = await seed_attempt(sessionmaker)
    eid = await recorded(runner, seeded.attempt_id)
    gate = asyncio.Event()
    real_work = runner._work  # noqa: SLF001

    async def gated(claimed: Any) -> Any:
        outcome = await real_work(claimed)  # graded successfully ...
        await gate.wait()  # ... but the finish waits while the lease expires
        return outcome

    runner._work = gated  # type: ignore[method-assign]  # noqa: SLF001
    task = asyncio.create_task(runner.grade(eid))
    await wait_until(lambda: _status_is(sessionmaker, seeded.attempt_id, "RUNNING"), timeout_s=10)
    await expire_lease(sessionmaker, eid)
    rec = build_reconciler(sessionmaker, queue, eval_queue, runner, [])
    assert await rec.step_recover_evaluations() == [seeded.attempt_id]  # E9a
    gate.set()
    assert await task == eid

    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    # The stale E5 matched 0 rows: still the reconciler's PENDING at epoch 2, no score.
    assert (ev.status, ev.lease_epoch, ev.error_reason) == ("PENDING", 2, "lease_expired")
    assert ev.task_success is None and await scores_of(sessionmaker, eid) == []
    # And the next claim grades it properly.
    await make_due(sessionmaker, eid)
    runner._work = real_work  # type: ignore[method-assign]  # noqa: SLF001
    assert await runner.grade(eid) == eid
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.lease_epoch, ev.eval_attempts) == ("SUCCEEDED", 3, 2)
    assert len(await scores_of(sessionmaker, eid)) == 1


async def _status_is(sm: SM, attempt_id: uuid.UUID, status: str) -> bool:
    try:
        return (await evaluation_of(sm, attempt_id)).status == status
    except Exception:
        return False


# -- lease loss ----------------------------------------------------------------------------------


async def test_lease_loss_stops_grading_kills_the_container_and_writes_nothing(
    sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    leases = FakeLeases()
    kills: list[str] = []
    runner = runner_for(sessionmaker, artifact_store, leases=leases, kills=kills)
    seeded = await seed_attempt(sessionmaker)
    eid = await recorded(runner, seeded.attempt_id)
    never = asyncio.Event()

    async def stuck(claimed: Any) -> Any:
        await never.wait()

    runner._work = stuck  # type: ignore[method-assign]  # noqa: SLF001
    task = asyncio.create_task(runner.grade(eid))
    await wait_until(lambda: _status_is(sessionmaker, seeded.attempt_id, "RUNNING"), timeout_s=10)
    await wait_until(_always(lambda: ("evaluation", str(eid)) in leases.held), timeout_s=5)
    _, on_lost = leases.held[("evaluation", str(eid))]
    on_lost()  # what the LeaseManager does on a `fenced` beat or a missed deadline
    assert await asyncio.wait_for(task, 10) == eid

    await asyncio.sleep(0.1)  # the kill runs on a thread
    assert kills == [str(seeded.attempt_id)]
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    # Nothing was written: still the claim's RUNNING row; the reconciler owns it now.
    assert (ev.status, ev.lease_epoch, ev.error_reason, ev.finished_at) == (
        "RUNNING",
        1,
        None,
        None,
    )
    assert await scores_of(sessionmaker, eid) == []
    assert leases.released == [("evaluation", str(eid), 1)]


def _always(fn: Callable[[], bool]) -> Callable[[], Any]:
    async def check() -> bool:
        return fn()

    return check


@pytest.mark.docker
async def test_lease_loss_removes_the_real_evaluation_container(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await _real_attempt(sessionmaker, queue, artifact_store, GIVE_UP, SLOW_SPEC)
    attempt = await _finished_attempt(sessionmaker, run_id)
    leases = FakeLeases()
    runner = runner_for(sessionmaker, artifact_store, leases=leases)
    runner._kill_by_attempt_label = DockerSandbox(docker.from_env()).kill_attempt  # noqa: SLF001
    eid = await recorded(runner, attempt.id)
    task = asyncio.create_task(runner.grade(eid))

    async def container_up() -> bool:
        return bool(leftover_containers(run_id))

    await wait_until(container_up, timeout_s=60, interval=0.2)
    _, on_lost = leases.held[("evaluation", str(eid))]
    on_lost()
    assert await asyncio.wait_for(task, 60) == eid

    # Two removals race here by design (the lease-loss kill, on its own thread,
    # and grading's own cleanup); Docker answers the loser "removal already in
    # progress", so the container can still be listed for a moment after
    # grade() returns. What matters is that it goes, not when the listing
    # catches up.
    async def containers_gone() -> bool:
        return not leftover_containers(run_id)

    await wait_until(containers_gone, timeout_s=30, interval=0.2)
    ev = await evaluation_of(sessionmaker, attempt.id)
    assert (ev.status, ev.lease_epoch, ev.finished_at) == ("RUNNING", 1, None)
    assert await scores_of(sessionmaker, eid) == []


# -- A1 recovery -------------------------------------------------------------------------------


async def test_a1_recovery_records_old_finished_attempts_and_publishes_pending(
    sessionmaker: SM, queue: RunQueue, eval_queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    old = await seed_attempt(sessionmaker, finished_ago_s=120)
    old_skipped = await seed_attempt(sessionmaker, spec=None, finished_ago_s=120)
    young = await seed_attempt(sessionmaker, finished_ago_s=5)
    rec = build_reconciler(sessionmaker, queue, eval_queue, runner, [], a1_grace_s=60)

    created = await rec.step_record_missing_evaluations()
    assert len(created) == 2
    pending = await evaluation_of(sessionmaker, old.attempt_id)
    assert (pending.status, pending.max_eval_attempts) == ("PENDING", 3)
    assert (await evaluation_of(sessionmaker, old_skipped.attempt_id)).status == "SKIPPED"
    async with sessionmaker() as s:  # the young attempt is the fast path's: left alone
        assert (
            await s.execute(
                text("SELECT count(*) FROM attempt_evaluations WHERE attempt_id = :a"),
                {"a": young.attempt_id},
            )
        ).scalar_one() == 0
    # The PENDING row was published to the evaluations stream and stamped.
    [msg] = await eval_queue.read("probe", count=5, block_ms=200)
    assert msg.evaluation_id == pending.id and msg.run_id is None
    assert (await evaluation_of(sessionmaker, old.attempt_id)).dispatched_at is not None
    assert await rec.step_record_missing_evaluations() == []  # nothing left to record


async def test_a1_ignores_command_tasks_and_unfinished_attempts(
    sessionmaker: SM, engine: Any, queue: RunQueue, eval_queue: RunQueue,
    artifact_store: LocalArtifactStore,
) -> None:  # fmt: skip
    runner = runner_for(sessionmaker, artifact_store)
    _, running = await running_run(engine)
    _, command = await running_run(engine)  # a command task
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE run_attempts SET status = 'FINISHED', outcome = 'SUCCESS', exit_code = 0,"
                " duration_ms = 5, finished_at = now() - interval '5 minutes' WHERE id = :a"
            ),
            {"a": command},
        )
    rec = build_reconciler(sessionmaker, queue, eval_queue, runner, [], a1_grace_s=1)
    assert running and await rec.step_record_missing_evaluations() == []


async def test_a1_racing_the_fast_path_creates_exactly_one_row(
    sessionmaker: SM, queue: RunQueue, eval_queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    seeded = await seed_attempt(sessionmaker, finished_ago_s=120)
    r1 = build_reconciler(sessionmaker, queue, eval_queue, runner, [], a1_grace_s=60)
    r2 = build_reconciler(sessionmaker, queue, eval_queue, runner, [], a1_grace_s=60)
    results = await asyncio.gather(
        runner.record_attempt(seeded.attempt_id),  # the fast path
        runner.record_attempt(seeded.attempt_id),
        r1.step_record_missing_evaluations(),
        r2.step_record_missing_evaluations(),
    )
    fast = [r for r in results[:2] if r is not None]
    recovered = [x for r in results[2:] for x in r]  # type: ignore[union-attr]
    assert len(fast) + len(recovered) == 1  # exactly one creator, whoever it was
    async with sessionmaker() as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM attempt_evaluations WHERE attempt_id = :a"),
                {"a": seeded.attempt_id},
            )
        ).scalar_one() == 1


# -- dispatch (step 6), reclaim, and consumption ------------------------------------------------


async def test_dispatch_publishes_due_pending_rows_once_and_stamps_them(
    sessionmaker: SM, queue: RunQueue, eval_queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    fresh = await recorded(runner, (await seed_attempt(sessionmaker)).attempt_id)
    lost = await recorded(runner, (await seed_attempt(sessionmaker)).attempt_id)
    backoff = await recorded(runner, (await seed_attempt(sessionmaker)).attempt_id)
    due_retry = await recorded(runner, (await seed_attempt(sessionmaker)).attempt_id)
    claimed = await recorded(runner, (await seed_attempt(sessionmaker)).attempt_id)
    async with sessionmaker() as s:
        old = text("now() - interval '5 minutes'")
        await s.execute(
            update(AttemptEvaluation).where(AttemptEvaluation.id == lost).values(created_at=old)
        )
        await s.execute(  # a retry still in backoff
            update(AttemptEvaluation)
            .where(AttemptEvaluation.id == backoff)
            .values(created_at=old, next_attempt_at=text("now() + interval '1 hour'"),
                    eval_attempts=1, error_reason="sandbox_failed")
        )  # fmt: skip
        await s.execute(  # a retry whose backoff elapsed since its last publish
            update(AttemptEvaluation)
            .where(AttemptEvaluation.id == due_retry)
            .values(dispatched_at=text("now() - interval '10 seconds'"),
                    next_attempt_at=text("now() - interval '1 second'"),
                    eval_attempts=1, error_reason="sandbox_failed")
        )  # fmt: skip
        assert await EvaluationRepository(s).claim(claimed, "w-x") is not None
        await s.commit()
    rec = build_reconciler(sessionmaker, queue, eval_queue, runner, [], redispatch_after_s=60)

    published = await rec.step_dispatch_evaluations()
    assert set(published) == {lost, due_retry}  # not the fresh row (the fast path's), the
    # backoff row, or the claimed one
    assert await rec.step_dispatch_evaluations() == []  # stamped: not republished
    for eid in (lost, due_retry):
        async with sessionmaker() as s:
            row = await s.get(AttemptEvaluation, eid, populate_existing=True)
        assert row is not None and row.dispatched_at is not None
    msgs = await eval_queue.read("probe", count=10, block_ms=200)
    assert {m.evaluation_id for m in msgs} == {lost, due_retry}
    assert fresh not in {m.evaluation_id for m in msgs}


async def test_read_any_pairs_each_message_with_its_stream(
    queue: RunQueue, eval_queue: RunQueue
) -> None:
    run_id, evaluation_id = uuid.uuid4(), uuid.uuid4()
    await queue.publish(run_id)
    await eval_queue.publish(evaluation_id)
    got = await read_any([queue, eval_queue], "c1", count=5, block_ms=200)
    by_stream = {q.stream: m for q, m in got}
    assert by_stream[queue.stream].run_id == run_id
    assert by_stream[queue.stream].evaluation_id is None
    assert by_stream[eval_queue.stream].evaluation_id == evaluation_id
    assert by_stream[eval_queue.stream].run_id is None


async def test_workers_consume_the_evaluation_stream_and_duplicates_are_harmless(
    sessionmaker: SM,
    queue: RunQueue,
    eval_queue: RunQueue,
    artifact_store: LocalArtifactStore,
    redis: Redis,
) -> None:
    runner = runner_for(sessionmaker, artifact_store)
    seeded = await seed_attempt(sessionmaker)
    eid = await recorded(runner, seeded.attempt_id)
    await eval_queue.publish(eid)
    await eval_queue.publish(eid)  # a duplicate
    await redis.xadd(eval_queue.stream, {EVALUATION_FIELD: "not-a-uuid"})  # malformed
    await eval_queue.publish(uuid.uuid4())  # stale: no such row

    executor = Executor(
        sessionmaker,
        queue,
        lambda spec: None,
        "w-c",
        None,
        runner,
        evaluation_queue=eval_queue,  # type: ignore[arg-type,return-value]
    )
    consumer = Consumer(
        queue, executor, evaluation_queue=eval_queue, concurrency=1, block_ms=100,
        drain_timeout_s=10, cancel_grace_s=1,
    )  # fmt: skip
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))

    async def settled() -> bool:
        pending = await redis.xpending(eval_queue.stream, eval_queue.group)
        ev = await evaluation_of(sessionmaker, seeded.attempt_id)
        return ev.status == "SUCCEEDED" and pending["pending"] == 0

    await wait_until(settled, timeout_s=15, interval=0.1)
    stop.set()
    await asyncio.wait_for(task, 10)
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.eval_attempts, ev.lease_epoch, ev.worker_id) == (1, 1, "w-test")
    assert len(await scores_of(sessionmaker, eid)) == 1  # graded exactly once


async def test_reclaim_and_hygiene_cover_the_evaluation_stream(
    sessionmaker: SM, queue: RunQueue, eval_queue: RunQueue, redis: Redis
) -> None:
    await eval_queue.publish(uuid.uuid4())
    got = await eval_queue.read("w-ghost", count=1, block_ms=200)  # read, never acked
    assert len(got) == 1
    rec = build_reconciler(sessionmaker, queue, eval_queue, None, [], redispatch_after_s=0.05)
    await asyncio.sleep(0.15)
    assert await rec.step_reclaim_pending() == 1  # reclaimed and acked on the eval stream
    assert (await redis.xpending(eval_queue.stream, eval_queue.group))["pending"] == 0
    # The ghost consumer has no pending entries and no worker row: deleted from the stream.
    await asyncio.sleep(0.7)  # idle beyond 3 x ttl (0.6 s)
    _, deleted = await rec.step_workers()
    assert "w-ghost" in deleted
    assert "w-ghost" not in [c.name for c in await eval_queue.consumers()]


# -- a worker crash mid-grading ----------------------------------------------------------------


async def _real_attempt(
    sm: SM,
    queue: RunQueue,
    store: LocalArtifactStore,
    script: list[dict[str, Any]],
    spec: dict[str, Any],
) -> uuid.UUID:
    """Run a real agent attempt (container, snapshot saved on the volume) with a
    stand-in that skips evaluation, so the attempt is FINISHED with no evaluation
    row, as after a crash between the finish and tx A. Returns the run id."""
    run_id = await make_run(sm, queue, script, evaluation=spec)
    ex, _ = build(sm, queue, store)

    async def skip(task: Task, attempt: RunAttempt) -> None:
        return None

    ex._evaluation_runner = type("Skip", (), {"evaluate": staticmethod(skip)})()  # type: ignore[assignment]  # noqa: SLF001
    await handle_next(ex, queue)
    return run_id


async def _finished_attempt(sm: SM, run_id: uuid.UUID) -> RunAttempt:
    async with sm() as s:
        return (await s.execute(select(RunAttempt).where(RunAttempt.run_id == run_id))).scalar_one()


TIMING = LeaseTiming(
    ttl_s=4.0, heartbeat_s=0.5, margin_s=1.0, reconcile_interval_s=0.2, liveness_timeout_s=1.5
)


async def test_the_keeper_renews_a_grading_lease_past_its_ttl_and_the_reconciler_leaves_it(
    sessionmaker: SM,
    queue: RunQueue,
    eval_queue: RunQueue,
    settings: Settings,
    artifact_store: LocalArtifactStore,
) -> None:
    """The keeper's `evaluation` kind renews attempt_evaluations rows (a latent
    SQL error in its IN-list is exactly what this would have caught)."""
    leases = LeaseManager(settings.database_url, TIMING, "w-long")
    await leases.start()
    try:
        runner = runner_for(sessionmaker, artifact_store, leases=leases, lease_ttl_s=TIMING.ttl_s)
        seeded = await seed_attempt(sessionmaker)
        eid = await recorded(runner, seeded.attempt_id)
        real_work = runner._work  # noqa: SLF001

        async def slow(claimed: Any) -> Any:
            out = await real_work(claimed)
            await asyncio.sleep(TIMING.ttl_s + 4)  # well past the first lease's expiry
            return out

        runner._work = slow  # type: ignore[method-assign]  # noqa: SLF001
        rec = build_reconciler(sessionmaker, queue, eval_queue, runner, [])
        task = asyncio.create_task(runner.grade(eid))
        deadline = time.monotonic() + TIMING.ttl_s + 1.5
        while time.monotonic() < deadline:
            assert await rec.step_recover_evaluations() == []  # never recovered
            await asyncio.sleep(0.3)
        ev = await evaluation_of(sessionmaker, seeded.attempt_id)
        assert ev.status == "RUNNING" and ev.lease_epoch == 1  # still ours, past the ttl
        assert await asyncio.wait_for(task, 20) == eid
    finally:
        await leases.stop()
    ev = await evaluation_of(sessionmaker, seeded.attempt_id)
    assert (ev.status, ev.lease_epoch, ev.eval_attempts) == ("SUCCEEDED", 1, 1)


@pytest.mark.docker
async def test_a_killed_grader_is_recovered_and_graded_by_another_worker(
    sessionmaker: SM,
    queue: RunQueue,
    eval_queue: RunQueue,
    settings: Settings,
    artifact_store: LocalArtifactStore,
    tmp_path: Path,
) -> None:
    # A real agent attempt whose (slow) grading the victim will be killed in.
    run_id = await _real_attempt(sessionmaker, queue, artifact_store, GIVE_UP, SLOW_SPEC)
    attempt = await _finished_attempt(sessionmaker, run_id)
    eid = await recorded(runner_for(sessionmaker, artifact_store, budget=3), attempt.id)
    await eval_queue.publish(eid)

    victim = await asyncio.create_subprocess_exec(
        sys.executable,
        HARNESS,
        settings.database_url,
        settings.redis_url,
        "w-victim",
        str(TIMING.ttl_s),
        str(TIMING.heartbeat_s),
        str(TIMING.margin_s),
        str(tmp_path / "artifacts"),
    )
    rescuer_leases: LeaseManager | None = None
    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    try:

        async def grading_in_container() -> bool:
            return await _status_is(sessionmaker, attempt.id, "RUNNING") and bool(
                leftover_containers(run_id)
            )

        await wait_until(grading_in_container, timeout_s=90, interval=0.2)
        victim.send_signal(signal.SIGKILL)
        await victim.wait()
        assert bool(leftover_containers(run_id))  # the crash left the container behind
        # Grading loads the spec from the database, so a fast spec replaces the
        # slow one for the retry (the victim's copy is gone with its process).
        async with sessionmaker() as s:
            task_id = (await s.get(Run, run_id)).task_id  # type: ignore[union-attr]
            await s.execute(update(Task).where(Task.id == task_id).values(evaluation=HIDDEN_SPEC))
            await s.commit()

        client = docker.from_env()
        sandbox = DockerSandbox(client)
        rescuer_leases = LeaseManager(settings.database_url, TIMING, "w-rescuer")
        await rescuer_leases.start()
        runner = EvaluationRunner(
            sessionmaker=sessionmaker,
            docker_client=client,
            artifact_store=artifact_store,
            worker_id="w-rescuer",
            eval_max_attempts=3,
            lease_manager=rescuer_leases,
            lease_ttl_s=TIMING.ttl_s,
            kill_by_attempt_label=sandbox.kill_attempt,
        )
        executor = Executor(
            sessionmaker, queue, sandbox.run, "w-rescuer", None, runner, evaluation_queue=eval_queue
        )
        reconciler = Reconciler(
            sessionmaker,
            queue,
            "w-rescuer",
            interval_s=TIMING.reconcile_interval_s,
            lease_ttl_s=TIMING.ttl_s,
            redispatch_after_s=60.0,
            orphan_sweep_interval_s=300.0,
            kill_attempt=sandbox.kill_attempt,
            sweep_orphans=sandbox.sweep_orphans,
            evaluation_queue=eval_queue,
            record_attempt=runner.record_attempt,
        )
        consumer = Consumer(
            queue, executor, evaluation_queue=eval_queue, concurrency=1, block_ms=200,
            drain_timeout_s=30, cancel_grace_s=5,
        )  # fmt: skip
        tasks = [
            asyncio.create_task(reconciler.run(stop)),
            asyncio.create_task(consumer.run(stop)),
        ]

        async def graded() -> bool:
            return await _status_is(sessionmaker, attempt.id, "SUCCEEDED")

        await wait_until(graded, timeout_s=120, interval=0.3)
    finally:
        if victim.returncode is None:
            victim.kill()
            await victim.wait()
        stop.set()
        for t in tasks:
            await asyncio.wait_for(t, 40)
        if rescuer_leases is not None:
            await rescuer_leases.stop()

    ev = await evaluation_of(sessionmaker, attempt.id)
    # claim (epoch 1, the victim) -> E9a (epoch 2) -> claim (epoch 3, the rescuer)
    assert (ev.status, ev.failure_category, ev.eval_attempts, ev.lease_epoch) == (
        "SUCCEEDED",
        "tests_failed",  # GIVE_UP never fixes the bug: graded, not errored
        2,
        3,
    )
    assert (ev.worker_id, ev.error_reason, ev.lease_expires_at) == ("w-rescuer", None, None)
    assert len(await scores_of(sessionmaker, eid)) == 1
    assert leftover_containers(run_id) == []  # the reconciler killed the victim's container
    await assert_g9(sessionmaker)
