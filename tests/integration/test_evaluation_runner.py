"""Phase 4 step 5: agent attempts graded by the EvaluationRunner through the
real executor - Redis dispatch, Postgres, a real agent container, the
snapshot, a fresh evaluation container, Step 3 grading, persistence.
FakeProvider only: no network, no API key.

Invariant checked throughout: evaluation never writes runs, run_attempts or
trajectory_events - the attempt outcome is authoritative and independent.
"""

import asyncio
import sys
import uuid
from decimal import Decimal
from typing import Any

import docker
import pytest
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.evaluation.classify import Verdict
from agenteval_core.evaluation.public import public_view
from agenteval_core.evaluation.results import CheckResult
from agenteval_core.evaluation.scoring import (
    DEFAULT_SCORING_CONFIG,
    ScoringConfig,
    ScoringWeights,
)
from agenteval_core.evaluation.taxonomy import EvaluationStatus, FailureCategory
from agenteval_core.hashing import sha256_hex
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
    TrajectoryEvent,
)
from agenteval_core.providers.fake import FakeProvider, FakeSession
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.repositories.runs import RunRepository
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.evaluation_runner import EvaluationRunner
from agenteval_worker.executor import Executor
from agenteval_worker.sandbox import evaluation as eval_sandbox
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox
from agenteval_worker.sandbox.session import SandboxError
from tests.integration.conftest import SANDBOX_IMAGE
from tests.integration.test_agent_executor import (
    BUGGY,
    FAST_RETRY,
    FIX_THE_BUG,
    FIXED,
    TESTS,
    call,
    handle_next,
    leftover_containers,
    turn,
)

pytestmark = pytest.mark.docker

SM = async_sessionmaker[AsyncSession]
MARKER = "HIDDEN_MARKER_7f3a"
HIDDEN_TESTS = (
    f"# {MARKER}\nimport unittest\nfrom calc import add\n\n"
    "class Hidden(unittest.TestCase):\n"
    f"    def test_pos(self):\n        self.assertEqual(add(2, 3), 5, '{MARKER}')\n"
    f"    def test_neg(self):\n        self.assertEqual(add(-1, 1), 0, '{MARKER}')\n"
)
HIDDEN_SPEC: dict[str, Any] = {
    "checks": [{"kind": "python_tests", "name": "hidden", "pattern": "test_h*.py",
                "expected_tests": 2}],
    "hidden_files": {"test_hidden.py": HIDDEN_TESTS},
    "protected_paths": ["test_calc.py"],
}  # fmt: skip
GIVE_UP = [turn("I could not fix it.")]


class RecordingFake(FakeProvider):
    """FakeProvider that keeps its sessions: the transcript is everything the
    model was shown (D11's model-visible surface)."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[FakeSession] = []

    def start(self, **kw: Any) -> FakeSession:
        session = super().start(**kw)
        self.sessions.append(session)
        return session


async def make_run(
    sm: SM,
    queue: RunQueue,
    script: list[dict[str, Any]],
    *,
    evaluation: dict[str, Any] | None = HIDDEN_SPEC,
    limits: dict[str, Any] | None = None,
    image: str = SANDBOX_IMAGE,
    scoring_config: ScoringConfig | None = None,
) -> uuid.UUID:
    async with sm() as s:
        agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}")
        s.add(agent)
        await s.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            model_provider="fake",
            model="fake-model",
            system_prompt="You fix bugs in small Python projects.",
            tools=["read_file", "write_file", "list_dir", "search", "shell", "run_tests"],
            shell_allowlist=["python"],
            limits=limits or {"max_steps": 10},
            model_settings={"script": script},
            config_hash=uuid.uuid4().hex * 2,
        )
        task = Task(
            name="fix-add",
            task_type="agent",
            image=image,
            instructions="The tests in test_calc.py fail. Fix calc.py so they pass.",
            test_command=["python", "-m", "unittest", "-q"],
            files={"calc.py": BUGGY, "test_calc.py": TESTS},
            timeout_s=120,
            memory_mb=256,
            cpu_limit=1.0,
            evaluation=evaluation,
        )
        s.add_all([version, task])
        await s.flush()
        batch_id = None
        if scoring_config is not None:
            dataset = Dataset(name=f"d-{uuid.uuid4().hex[:6]}", version="1", status="DRAFT")
            s.add(dataset)
            await s.flush()
            batch = Evaluation(
                name="batch",
                dataset_id=dataset.id,
                agent_version_id=version.id,
                repetitions=1,
                scoring_config=scoring_config.model_dump(mode="json"),
                scoring_config_hash=scoring_config.config_hash(),
            )
            s.add(batch)
            await s.flush()
            batch_id = batch.id
        run = Run(
            task_id=task.id,
            agent_version_id=version.id,
            evaluation_id=batch_id,
            repetition=1 if batch_id else None,
            status="QUEUED",
            attempt_count=0,
        )
        s.add(run)
        await s.commit()
        run_id = run.id
    await queue.publish(run_id)
    return run_id


class Invariant:
    """Wraps EvaluationRunner.evaluate: records the run, attempt and
    trajectory rows just before evaluation, for comparison afterwards."""

    def __init__(self, sm: SM, runner: EvaluationRunner) -> None:
        self.sm, self.runner = sm, runner
        self.before: dict[str, Any] | None = None
        real = runner.evaluate

        async def evaluate(task: Task, attempt: RunAttempt) -> Any:
            self.before = await durable_state(sm, attempt.run_id)
            return await real(task, attempt)

        runner.evaluate = evaluate  # type: ignore[method-assign]


async def durable_state(sm: SM, run_id: uuid.UUID) -> dict[str, Any]:
    def cols(row: Any) -> dict[str, Any]:
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}

    async with sm() as s:
        run = await s.get(Run, run_id, populate_existing=True)
        attempts = (
            await s.execute(select(RunAttempt).where(RunAttempt.run_id == run_id))
        ).scalars()
        attempt_rows = [cols(a) for a in attempts]
        events = (
            await s.execute(
                select(TrajectoryEvent)
                .join(RunAttempt, RunAttempt.id == TrajectoryEvent.attempt_id)
                .where(RunAttempt.run_id == run_id)
                .order_by(TrajectoryEvent.seq)
            )
        ).scalars()
        return {"run": cols(run), "attempts": attempt_rows, "events": [cols(e) for e in events]}


def build(
    sm: SM,
    queue: RunQueue,
    store: LocalArtifactStore,
    *,
    provider: FakeProvider | None = None,
    agent_kw: dict[str, Any] | None = None,
    eval_kw: dict[str, Any] | None = None,
) -> tuple[Executor, Invariant]:
    client = docker.from_env()
    agent_runner = AgentRunner(
        sessionmaker=sm,
        docker_client=client,
        providers={"fake": provider or FakeProvider()},
        retry_policy=FAST_RETRY,
        **(agent_kw or {}),
    )
    runner = EvaluationRunner(
        sessionmaker=sm, docker_client=client, artifact_store=store, **(eval_kw or {})
    )
    invariant = Invariant(sm, runner)
    ex = Executor(
        sm, queue, DockerSandbox(client).run, "w-eval", agent_runner, runner, artifact_store=store
    )
    return ex, invariant


async def results(
    sm: SM, run_id: uuid.UUID
) -> tuple[Run, RunAttempt, AttemptEvaluation | None, list[Score], AttemptArtifact | None]:
    async with sm() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None
        [attempt] = await RunRepository(s).list_attempts(run_id)
        ev = (
            await s.execute(
                select(AttemptEvaluation).where(AttemptEvaluation.attempt_id == attempt.id)
            )
        ).scalar_one_or_none()
        scores = []
        if ev is not None:
            scores = list(
                (
                    await s.execute(select(Score).where(Score.attempt_evaluation_id == ev.id))
                ).scalars()
            )
        artifact = (
            await s.execute(select(AttemptArtifact).where(AttemptArtifact.attempt_id == attempt.id))
        ).scalar_one_or_none()
        return run, attempt, ev, scores, artifact


async def assert_untouched(sm: SM, run_id: uuid.UUID, inv: Invariant) -> None:
    assert inv.before is not None, "evaluation never ran"
    assert await durable_state(sm, run_id) == inv.before


# -- grading outcomes -------------------------------------------------------------------------


async def test_fixed_bug_is_graded_passed_scored_and_cleaned_up(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, attempt, ev, [score], artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", True, "passed")
    assert ev.created_at >= attempt.finished_at  # D1: only after the durable finish
    assert ev.checks is not None and [c["name"] for c in ev.checks] == ["hidden"]
    assert (ev.checks[0]["tests_passed"], ev.checks[0]["tests_total"]) == (2, 2)
    assert ev.integrity == {"ok": True, "tampered": {}, "normalized_paths": []}
    assert ev.metrics is not None and ev.metrics["ran_tests"] is True
    # v1: 0.6 + 0.2 + 0.1 * reliability(1) + 0.1 * efficiency, where efficiency =
    # 1 - mean(steps 4/10, cost $0.0008/$1.00) = 0.7996 (FakeProvider's nominal price)
    assert score.scoring_config_hash == DEFAULT_SCORING_CONFIG.config_hash()
    assert score.overall == Decimal("0.97996")
    # Step 6: the snapshot is a file on the artifact volume, saved before finish.
    assert artifact is not None and artifact.content is None and artifact.storage_key is not None
    assert attempt.workspace_capture == "stored"
    data = await artifact_store.read(artifact.storage_key, artifact.size_bytes, artifact.sha256)
    assert artifact.sha256 == sha256_hex(data)
    assert artifact.artifact_metadata == {"normalized_paths": []}
    assert attempt.sandbox_image_id is not None and attempt.sandbox_image_id.startswith("sha256:")
    assert leftover_containers(run_id) == []
    await assert_untouched(sessionmaker, run_id, inv)


async def test_unfixed_bug_is_a_candidate_failure(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, GIVE_UP)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, _, ev, [score], _ = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", False, "tests_failed")
    assert "never_ran_tests" in (ev.failure_tags or [])
    assert score.overall < Decimal("0.6")
    await assert_untouched(sessionmaker, run_id, inv)


async def test_rewriting_a_protected_test_is_tampering_with_score_zero(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    cheat = [
        turn(
            tool_calls=[
                call("write_file", path="calc.py", content=FIXED),
                call("write_file", path="test_calc.py", content="import unittest\n"),
            ]
        ),
        turn("Done."),
    ]
    run_id = await make_run(sessionmaker, queue, cheat)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, _, ev, [score], _ = await results(sessionmaker, run_id)
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", False, "tampering")
    assert ev.checks is not None and ev.checks[0]["passed"] is True  # hidden tests still ran
    assert ev.integrity is not None and ev.integrity["tampered"] == {
        "test_calc.py": "content changed"
    }
    assert score.overall == 0
    await assert_untouched(sessionmaker, run_id, inv)


async def test_a_limit_outcome_is_graded_with_its_limit_category(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    looping = [turn(tool_calls=[call("list_dir")]) for _ in range(5)]
    run_id = await make_run(sessionmaker, queue, looping, limits={"max_steps": 2})
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, _, ev, scores, artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "MAX_STEPS")
    assert ev is not None
    assert (ev.status, ev.failure_category) == ("SUCCEEDED", "max_steps")
    assert "tests_failed" in (ev.failure_tags or [])
    assert len(scores) == 1 and artifact is not None
    await assert_untouched(sessionmaker, run_id, inv)


# -- not graded ----------------------------------------------------------------------------------


async def test_platform_failure_is_skipped_without_a_snapshot(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, image="agenteval-missing:none")
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, _, ev, scores, artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("FAILED", "SANDBOX_ERROR")
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == (
        "SKIPPED",
        None,
        "platform_sandbox",
    )
    assert scores == [] and artifact is None
    await assert_untouched(sessionmaker, run_id, inv)


async def test_task_without_a_spec_is_skipped_without_a_snapshot(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, evaluation=None)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, _, ev, scores, artifact = await results(sessionmaker, run_id)
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == ("SKIPPED", None, None)
    assert scores == [] and artifact is None
    await assert_untouched(sessionmaker, run_id, inv)


async def test_final_answer_only_spec_opens_no_container(
    sessionmaker: SM,
    queue: RunQueue,
    monkeypatch: pytest.MonkeyPatch,
    artifact_store: LocalArtifactStore,
) -> None:
    """A6: graded from the attempt row alone; no snapshot, no container."""

    def forbidden(*a: Any, **kw: Any) -> Any:
        raise AssertionError("A6: no evaluation container for final-answer-only specs")

    monkeypatch.setattr(eval_sandbox.EvaluationSession, "open", forbidden)
    spec = {"checks": [{"kind": "final_answer", "name": "a", "match": "contains",
                        "expected": "sign"}]}  # fmt: skip
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, evaluation=spec)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, _, ev, [_], artifact = await results(sessionmaker, run_id)
    assert ev is not None
    assert (ev.status, ev.failure_category) == ("SUCCEEDED", "passed")
    assert artifact is None
    await assert_untouched(sessionmaker, run_id, inv)


# -- evaluator failures are not candidate failures -------------------------------------------------


async def test_evaluation_container_failure_is_an_evaluator_error(
    sessionmaker: SM,
    queue: RunQueue,
    monkeypatch: pytest.MonkeyPatch,
    artifact_store: LocalArtifactStore,
) -> None:
    def broken(*a: Any, **kw: Any) -> Any:
        raise SandboxError("docker error: boom", reason=FailureReason.SANDBOX_CREATE_FAILED)

    monkeypatch.setattr(eval_sandbox.EvaluationSession, "open", broken)
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, _, ev, scores, artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == ("ERROR", None, "evaluation_error")
    assert ev.error_message is not None and "boom" in ev.error_message
    assert scores == []
    assert artifact is not None  # kept: the attempt stays re-checkable
    await assert_untouched(sessionmaker, run_id, inv)


async def test_snapshot_over_the_cap_is_an_evaluator_error(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """A2: a capture failure leaves the attempt COMPLETED; never the agent's fault."""
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, inv = build(sessionmaker, queue, artifact_store, agent_kw={"snapshot_max_bytes": 16})
    await handle_next(ex, queue)

    run, attempt, ev, scores, artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert ev is not None
    assert (ev.status, ev.failure_category) == ("ERROR", "evaluation_error")
    assert ev.error_message is not None and "too_large" in ev.error_message
    assert ev.error_reason == "capture_too_large"
    assert attempt.workspace_capture == "too_large"
    assert scores == [] and artifact is None
    assert leftover_containers(run_id) == []
    await assert_untouched(sessionmaker, run_id, inv)


async def test_evaluation_past_its_budget_is_an_evaluator_error_and_cleaned_up(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    spec = {"checks": [{"kind": "io", "name": "slow", "argv": ["python", "-c",
                        "import time; time.sleep(60)"], "expected_stdout": "", "timeout_s": 10}],
            "timeout_s": 10}  # fmt: skip
    run_id = await make_run(sessionmaker, queue, GIVE_UP, evaluation=spec)
    # No setup grace: the overall budget (10s) runs out before the check's own 10s.
    ex, inv = build(sessionmaker, queue, artifact_store, eval_kw={"setup_grace_s": 0})
    await handle_next(ex, queue)

    _, _, ev, scores, _ = await results(sessionmaker, run_id)
    assert ev is not None
    assert (ev.status, ev.failure_category) == ("ERROR", "evaluation_error")
    assert ev.error_message == "evaluation did not finish within 10s"
    assert scores == []
    assert leftover_containers(run_id) == []
    await assert_untouched(sessionmaker, run_id, inv)


# -- idempotency and fencing -----------------------------------------------------------------------


async def test_duplicate_and_concurrent_evaluations_grade_once(
    sessionmaker: SM,
    queue: RunQueue,
    monkeypatch: pytest.MonkeyPatch,
    artifact_store: LocalArtifactStore,
) -> None:
    captured: list[tuple[Task, RunAttempt]] = []
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, inv = build(sessionmaker, queue, artifact_store)
    real = inv.runner

    async def record(task: Task, attempt: RunAttempt) -> None:
        captured.append((task, attempt))

    ex._evaluation_runner = type("Rec", (), {"evaluate": staticmethod(record)})()  # type: ignore[assignment]
    await handle_next(ex, queue)
    [(task, attempt)] = captured

    opened = 0
    real_open = eval_sandbox.EvaluationSession.open.__func__  # type: ignore[attr-defined]

    def counting_open(cls: Any, *a: Any, **kw: Any) -> Any:
        nonlocal opened
        opened += 1
        return real_open(cls, *a, **kw)

    monkeypatch.setattr(eval_sandbox.EvaluationSession, "open", classmethod(counting_open))
    ids = await asyncio.gather(*(real.evaluate(task, attempt) for _ in range(3)))
    again = await real.evaluate(task, attempt)

    assert sum(i is not None for i in ids) == 1 and again is None
    assert opened == 1
    _, _, ev, scores, _ = await results(sessionmaker, run_id)
    assert ev is not None and ev.status == "SUCCEEDED" and len(scores) == 1
    assert leftover_containers(run_id) == []


async def test_a_fenced_attempt_is_never_evaluated(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    slow = [turn(tool_calls=[call("list_dir")]), turn("never recorded", delay_s=1.0)]
    run_id = await make_run(sessionmaker, queue, slow)
    ex, inv = build(sessionmaker, queue, artifact_store)
    task = asyncio.create_task(handle_next(ex, queue))
    async with asyncio.timeout(20):
        while not (await durable_state(sessionmaker, run_id))["events"]:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    async with sessionmaker() as s:  # ownership moves: the save and finish_attempt are fenced
        # Step 6: the save fences on the ATTEMPT row's epoch (ADR-008 B, invariant
        # I1: a RUNNING attempt and its run share an epoch), so the attempt's epoch
        # moves with the run's, as recovery would leave them. Moving only the run's
        # was an impossible state the old bytea flow never touched before finish.
        await s.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(worker_id="w-other", lease_epoch=Run.lease_epoch + 1)
        )
        await s.execute(
            update(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .values(lease_epoch=RunAttempt.lease_epoch + 1)
        )
        await s.commit()
    await asyncio.wait_for(task, 30)

    run, _, ev, _, artifact = await results(sessionmaker, run_id)
    assert run.status == "RUNNING"
    assert ev is None and artifact is None and inv.before is None
    # The save was fenced too (epoch moved): its file was removed, not orphaned.
    assert await artifact_store.list_files() == []
    assert leftover_containers(run_id) == []


async def test_a_batch_run_is_scored_with_the_batch_config(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    only_success = ScoringConfig(
        weights=ScoringWeights(
            task_success=Decimal(1), test_score=Decimal(0), reliability=Decimal(0),
            efficiency=Decimal(0),
        )
    )  # fmt: skip
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, scoring_config=only_success)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, _, ev, [score], _ = await results(sessionmaker, run_id)
    assert ev is not None and ev.status == "SUCCEEDED"
    assert score.scoring_config_hash == only_success.config_hash()
    assert score.overall == 1


# -- preserved Step 3/4 guarantees -----------------------------------------------------------------


@pytest.mark.parametrize(("script", "passed"), [(FIX_THE_BUG, True), (GIVE_UP, False)])
async def test_io_and_command_checks_through_the_runner(
    sessionmaker: SM,
    queue: RunQueue,
    script: list[dict[str, Any]],
    passed: bool,
    artifact_store: LocalArtifactStore,
) -> None:
    """C4: io output is compared on the host; C6: command checks only with
    trusted_environment. Both run as Step 3 ExecPlans in the eval container."""
    spec = {
        "checks": [
            {"kind": "io", "name": "io", "argv": ["python", "-c",
             "from calc import add; print(add(int(input()), 3))"], "stdin": "2\n",
             "expected_stdout": "5"},
            {"kind": "command", "name": "cmd", "argv": ["python", "-c",
             "import calc, sys; sys.exit(calc.add(1, 1) != 2)"], "trusted_environment": True},
        ],
    }  # fmt: skip
    run_id = await make_run(sessionmaker, queue, script, evaluation=spec)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, _, ev, _, _ = await results(sessionmaker, run_id)
    assert ev is not None and ev.status == "SUCCEEDED"
    assert ev.checks is not None
    assert [(c["name"], c["passed"]) for c in ev.checks] == [("io", passed), ("cmd", passed)]
    assert ev.task_success is passed
    await assert_untouched(sessionmaker, run_id, inv)


async def test_planted_sitecustomize_cannot_pass_the_hidden_tests(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """C2: the hardened invocation ignores agent-planted startup hooks."""
    plant = [
        turn(tool_calls=[call("write_file", path="sitecustomize.py",
                              content="import calc\ncalc.add = lambda a, b: a + b\n")]),
        turn("Done."),
    ]  # fmt: skip
    run_id = await make_run(sessionmaker, queue, plant)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, _, ev, _, _ = await results(sessionmaker, run_id)
    assert ev is not None
    assert (ev.status, ev.failure_category) == ("SUCCEEDED", "tests_failed")


async def test_hidden_content_never_reaches_storage_logs_model_or_public_view(
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D11. The failing hidden tests' traceback quotes the hidden file (and
    the marker); none of it may be stored, logged, shown to the model or
    projected outward."""
    # structlog caches a module logger under the config in force at its first use,
    # and every API lifespan re-configures with a new processor list that
    # capture_logs can't reach through an older cache. Whether an earlier test
    # already used a module's logger therefore depended on test-file ordering;
    # fresh, uncached loggers make this test independent of it.
    for name, module in list(sys.modules.items()):
        if name.startswith(("agenteval_core", "agenteval_worker")) and hasattr(module, "log"):
            monkeypatch.setattr(module, "log", structlog.get_logger(name))
    provider = RecordingFake()
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG[:1] + GIVE_UP)
    ex, inv = build(sessionmaker, queue, artifact_store, provider=provider)
    with structlog.testing.capture_logs() as logs:
        await handle_next(ex, queue)

    run, attempt, ev, scores, _ = await results(sessionmaker, run_id)
    assert ev is not None and ev.failure_category == "tests_failed"
    assert any(e["event"] == "evaluation_finished" for e in logs)  # capture works
    surfaces = {
        "evaluation": {c.key: getattr(ev, c.key) for c in ev.__table__.columns},
        "scores": [{c.key: getattr(s, c.key) for c in s.__table__.columns} for s in scores],
        "durable": await durable_state(sessionmaker, run_id),
        "logs": logs,
        "model": [sess.transcript for sess in provider.sessions],
    }
    for name, surface in surfaces.items():
        assert MARKER not in repr(surface), name
    assert ev.checks is not None
    verdict = Verdict(
        EvaluationStatus(ev.status), ev.task_success, FailureCategory(ev.failure_category),
        tuple(ev.failure_tags or ()), ev.failure_detail or "",
    )  # fmt: skip
    public = public_view(verdict, [CheckResult.model_validate(c) for c in ev.checks])
    dumped = public.model_dump_json()
    assert MARKER not in dumped and "hidden" not in dumped and "test_hidden" not in dumped
    assert provider.sessions and run.status == "COMPLETED" and attempt.final_answer


async def test_command_tasks_are_never_evaluated(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    async with sessionmaker() as s:
        task = Task(
            name="cmd", task_type="command", image=SANDBOX_IMAGE, command=["python", "-c", "1"],
            files={}, timeout_s=20, memory_mb=256, cpu_limit=1.0,
        )  # fmt: skip
        s.add(task)
        await s.flush()
        run = Run(task_id=task.id, status="QUEUED", attempt_count=0)
        s.add(run)
        await s.commit()
        run_id = run.id
    await queue.publish(run_id)
    ex, inv = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run_row, attempt, ev, _, artifact = await results(sessionmaker, run_id)
    assert (run_row.status, run_row.outcome) == ("COMPLETED", "SUCCESS")
    assert attempt.sandbox_image_id is None  # S1 unchanged
    assert attempt.workspace_capture is None  # ck_run_attempts_capture: agent tasks only
    assert ev is None and artifact is None and inv.before is None
