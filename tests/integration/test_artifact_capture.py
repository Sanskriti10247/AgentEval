"""O3d end to end (Step 6): the capture value an agent attempt finishes with,
and evaluation reading the snapshot back by its row (ADR-008 A).

Real executor, Redis, Postgres, an agent container, the artifact directory and
an evaluation container (FakeProvider: no network)."""

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore, StoredArtifact
from agenteval_core.models import (
    AttemptArtifact,
    AttemptEvaluation,
    Run,
    RunAttempt,
    Score,
    Task,
)
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.taxonomy import EvaluationErrorReason, FailureReason
from agenteval_worker.evaluation_runner import EvaluationRunner
from agenteval_worker.sandbox.evaluation import SnapshotError
from agenteval_worker.sandbox.session import SandboxError
from tests.integration.test_agent_executor import FIX_THE_BUG, handle_next, leftover_containers
from tests.integration.test_evaluation_runner import build, make_run, results

pytestmark = pytest.mark.docker

SM = async_sessionmaker[AsyncSession]
FINAL_ANSWER_ONLY: dict[str, Any] = {
    "checks": [{"kind": "final_answer", "name": "a", "match": "contains", "expected": "sign"}]
}


async def capture_of(sm: SM, run_id: uuid.UUID) -> str | None:
    _, attempt, *_ = await results(sm, run_id)
    return attempt.workspace_capture


# -- capture values through the executor ----------------------------------------------------------


async def test_stored_capture_has_a_row_a_file_and_no_bytea(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, attempt, ev, _, artifact = await results(sessionmaker, run_id)
    assert attempt.workspace_capture == "stored"
    assert artifact is not None and artifact.content is None
    assert [k for k, _ in await artifact_store.list_files()] == [artifact.storage_key]
    assert ev is not None and ev.status == "SUCCEEDED" and ev.error_reason is None


@pytest.mark.parametrize(
    ("raised", "capture"),
    [
        (SnapshotError("too_large", "x"), "too_large"),
        (SnapshotError("incomplete", "x"), "incomplete"),
        (SnapshotError("invalid", "x"), "invalid"),
        (SandboxError("docker said no", reason=FailureReason.SANDBOX_EXEC_FAILED), "incomplete"),
    ],
    ids=["too_large", "incomplete", "invalid", "other-export-failure"],
)
async def test_an_export_failure_records_its_kind_and_grades_as_that_capture_error(
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    capture: str,
) -> None:
    def failing_export(*a: Any, **kw: Any) -> Any:
        raise raised

    monkeypatch.setattr("agenteval_worker.agent_runner.export_workspace", failing_export)
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, attempt, ev, scores, artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")  # never the agent's fault
    assert attempt.workspace_capture == capture
    assert artifact is None and await artifact_store.list_files() == []
    assert ev is not None and (ev.status, ev.failure_category) == ("ERROR", "evaluation_error")
    assert ev.error_reason == f"capture_{capture}"
    assert ev.error_message is not None and f"capture_{capture}" in ev.error_message
    assert scores == [] and leftover_containers(run_id) == []


async def test_a_failed_save_is_persist_failed_and_never_changes_the_outcome(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    class DiskFull(LocalArtifactStore):
        async def put(self, attempt_id: uuid.UUID, kind: str, data: bytes) -> StoredArtifact:
            raise OSError("no space left on device")

    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    ex, _ = build(sessionmaker, queue, DiskFull(artifact_store.root))
    await handle_next(ex, queue)

    run, attempt, ev, scores, artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert attempt.workspace_capture == "persist_failed"
    assert artifact is None
    assert ev is not None and ev.status == "ERROR"
    assert ev.error_reason == "capture_persist_failed" and scores == []


async def test_a_spec_that_needs_no_workspace_is_not_needed(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, evaluation=FINAL_ANSWER_ONLY)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    _, attempt, ev, [_], artifact = await results(sessionmaker, run_id)
    assert attempt.workspace_capture == "not_needed" and artifact is None
    assert ev is not None and ev.status == "SUCCEEDED"
    assert await artifact_store.list_files() == []


async def test_a_task_without_a_spec_is_not_needed(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, evaluation=None)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)
    assert await capture_of(sessionmaker, run_id) == "not_needed"


async def test_a_failed_outcome_has_no_capture(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """ck_run_attempts_capture: only a completed outcome carries a value."""
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG, image="agenteval-missing:none")
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    run, attempt, *_ = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("FAILED", "SANDBOX_ERROR")
    assert attempt.workspace_capture is None and await artifact_store.list_files() == []


async def test_a_limit_outcome_is_still_captured(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    from tests.integration.test_agent_executor import call, turn

    looping = [turn(tool_calls=[call("list_dir")]) for _ in range(5)]
    run_id = await make_run(sessionmaker, queue, looping, limits={"max_steps": 2})
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)
    run, attempt, *_ = await results(sessionmaker, run_id)
    assert run.outcome == "MAX_STEPS" and attempt.workspace_capture == "stored"


# -- evaluation reads the snapshot by its row ------------------------------------------------------


async def run_and_grade(
    sm: SM, queue: RunQueue, store: LocalArtifactStore
) -> tuple[EvaluationRunner, uuid.UUID]:
    """An agent run that fixed the bug and was graded SUCCEEDED."""
    run_id = await make_run(sm, queue, FIX_THE_BUG)
    ex, inv = build(sm, queue, store)
    await handle_next(ex, queue)
    _, _, ev, *_ = await results(sm, run_id)
    assert ev is not None and ev.status == "SUCCEEDED"
    return inv.runner, run_id


async def regrade(sm: SM, runner: EvaluationRunner, run_id: uuid.UUID) -> AttemptEvaluation:
    """Forget the evaluation (not the attempt, not the artifact) and grade the
    attempt again from its rows alone - what any other worker would do."""
    async with sm() as s:
        [attempt] = (
            await s.execute(select(RunAttempt).where(RunAttempt.run_id == run_id))
        ).scalars()
        await s.execute(
            delete(Score).where(
                Score.attempt_evaluation_id.in_(
                    select(AttemptEvaluation.id).where(AttemptEvaluation.attempt_id == attempt.id)
                )
            )
        )
        await s.execute(delete(AttemptEvaluation).where(AttemptEvaluation.attempt_id == attempt.id))
        run = await s.get(Run, run_id)
        assert run is not None
        task = await s.get(Task, run.task_id)
        assert task is not None
        await s.commit()
    assert await runner.evaluate(task, attempt) is not None
    _, _, ev, *_ = await results(sm, run_id)
    assert ev is not None
    return ev


async def artifact_row(sm: SM, run_id: uuid.UUID) -> AttemptArtifact:
    _, _, _, _, artifact = await results(sm, run_id)
    assert artifact is not None
    return artifact


async def test_grading_reads_the_storage_key_row_and_verifies_it(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    ev = await regrade(sessionmaker, runner, run_id)  # a fresh read of the file
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", True, "passed")
    assert ev.error_reason is None


async def test_a_legacy_content_row_is_still_graded(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """Phase 4 rows keep their bytes in `content`; there is no file at all."""
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    row = await artifact_row(sessionmaker, run_id)
    assert row.storage_key is not None
    data = await artifact_store.read(row.storage_key, row.size_bytes, row.sha256)
    async with sessionmaker() as s:
        await s.execute(
            update(AttemptArtifact)
            .where(AttemptArtifact.id == row.id)
            .values(content=data, storage_key=None)
        )
        await s.commit()
    await artifact_store.delete(row.storage_key)

    ev = await regrade(sessionmaker, runner, run_id)
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", True, "passed")


async def test_a_missing_file_is_a_platform_error_never_a_grade(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    row = await artifact_row(sessionmaker, run_id)
    assert row.storage_key is not None
    await artifact_store.delete(row.storage_key)

    ev = await regrade(sessionmaker, runner, run_id)
    assert (ev.status, ev.task_success, ev.failure_category) == ("ERROR", None, "evaluation_error")
    assert ev.error_reason == EvaluationErrorReason.ARTIFACT_MISSING.value
    assert ev.error_message is not None and "artifact_missing" in ev.error_message
    assert row.storage_key not in ev.error_message  # D11: the reason's name only
    assert leftover_containers(run_id) == []


@pytest.mark.parametrize("damage", ["flipped-bytes", "truncated"])
async def test_a_corrupt_file_is_a_platform_error_never_a_grade(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore, damage: str
) -> None:
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    row = await artifact_row(sessionmaker, run_id)
    assert row.storage_key is not None
    path = artifact_store.root / row.storage_key
    data = path.read_bytes()
    path.write_bytes(data[:-1] if damage == "truncated" else bytes(b ^ 0xFF for b in data))

    ev = await regrade(sessionmaker, runner, run_id)
    assert (ev.status, ev.failure_category) == ("ERROR", "evaluation_error")
    assert ev.error_reason == EvaluationErrorReason.ARTIFACT_CORRUPT.value
    assert ev.error_message is not None and "artifact_corrupt" in ev.error_message
    assert leftover_containers(run_id) == []


@pytest.mark.parametrize("capture", ["too_large", "incomplete", "invalid", "persist_failed"])
async def test_each_non_stored_capture_grades_as_its_capture_error(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore, capture: str
) -> None:
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    async with sessionmaker() as s:
        await s.execute(
            text("UPDATE run_attempts SET workspace_capture = :c WHERE run_id = :r"),
            {"c": capture, "r": run_id},
        )
        await s.commit()

    ev = await regrade(sessionmaker, runner, run_id)
    assert (ev.status, ev.failure_category) == ("ERROR", "evaluation_error")
    assert ev.error_reason == f"capture_{capture}"
    assert ev.error_message is not None and f"capture_{capture}" in ev.error_message


@pytest.mark.parametrize("capture", ["stored", "not_needed", None])
async def test_a_spec_that_needs_the_workspace_but_has_no_row_is_capture_missing(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore, capture: str | None
) -> None:
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    async with sessionmaker() as s:
        await s.execute(delete(AttemptArtifact))
        await s.execute(
            text("UPDATE run_attempts SET workspace_capture = :c WHERE run_id = :r"),
            {"c": capture, "r": run_id},
        )
        await s.commit()

    ev = await regrade(sessionmaker, runner, run_id)
    assert (ev.status, ev.failure_category) == ("ERROR", "evaluation_error")
    assert ev.error_reason == EvaluationErrorReason.CAPTURE_MISSING.value
    assert ev.error_message is not None and "capture_missing" in ev.error_message


async def test_integrity_normalized_paths_survive_the_round_trip_through_the_row(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """The snapshot's normalized_paths used to travel in memory; now they are
    the artifact row's metadata, so any worker's grading records them."""
    runner, run_id = await run_and_grade(sessionmaker, queue, artifact_store)
    async with sessionmaker() as s:
        await s.execute(
            update(AttemptArtifact).values(artifact_metadata={"normalized_paths": ["calc.py"]})
        )
        await s.commit()
    ev = await regrade(sessionmaker, runner, run_id)
    assert ev.integrity is not None and ev.integrity["normalized_paths"] == ["calc.py"]
