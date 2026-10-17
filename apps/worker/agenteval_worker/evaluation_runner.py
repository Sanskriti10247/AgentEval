"""EvaluationRunner: records and grades finished agent attempts (Phase 4
step 5; split and made recoverable in Phase 5 step 7).

Runs only after finish_attempt has COMMITTED (D1, A1), in its own
transactions, so evaluation can never roll back or alter the attempt's
outcome. Evaluation never writes runs, run_attempts or trajectory_events.

Two halves, so a different worker (or the reconciler) can finish what a dead
one started - everything `grade` needs is loaded from Postgres and the
artifact store, never handed over in memory:

    record(attempt)     tx A: the attempt's one evaluation row, ON CONFLICT DO NOTHING
                        (a duplicate recorder stops here), only for a FINISHED
                        attempt (F11), with the grading budget pinned (F9)
                          E1 PENDING   needs grading
                          E2 SKIPPED   platform failure (never fairly tested) or no spec
                          E3 ERROR     invalid spec or scoring config, or no usable
                                       workspace capture: non-retryable, with a reason
    grade(evaluation_id)
      E4 claim          PENDING -> RUNNING, epoch + 1, eval_attempts + 1, a lease the
                        keeper renews (kind "evaluation"); exactly one winner
         grade          fresh EvaluationSession (only if the spec needs the workspace,
                        A6): evidence from the pristine snapshot, static checks, then
                        Step 3 ExecPlans through EvaluationWorkspace; classify, score
      finish            fenced by (id, RUNNING, epoch), then the lease is released:
                          E5 SUCCEEDED (+ one scores row, same tx)
                          E6 ERROR     non-retryable reason, or budget spent
                          E7 PENDING   retryable reason, budget left: backoff
                          E8a/E8b      the worker was hard-cancelled mid-grading
                        A lease lost mid-grading cancels the work, kills the
                        evaluation container by its attempt label and writes nothing:
                        the reconciler (E9a/E9b) owns the row from then on.

The executor's fast path calls `evaluate` = record, then grade inline. The
reconciler dispatches rows the fast path never finished (a worker that died, a
retry whose backoff elapsed) on the evaluations stream; `grade` is the same code.

Failure attribution: anything the evaluator itself could not do - capture the
workspace, open/drive the container, finish within its budget - is ERROR /
evaluation_error, never a candidate failure. A check that runs past its OWN
timeout is the candidate's (judge() says tests_failed). The reason is a
structured EvaluationErrorReason chosen where the failure is raised, never
parsed from a message:

    spec_invalid, scoring_config_invalid        grading again cannot change that
    capture_*  (too_large | incomplete | invalid | persist_failed | missing),
    artifact_missing | artifact_corrupt         the snapshot row, file or capture state
    check_error                                 a check could not run (the verdict is ERROR)
    sandbox_failed, budget_timeout,
    internal_unexpected                         transient: retried while budget is left

Step 6 (O3d, ADR-008): the executor saved the snapshot BEFORE finish (T-artifact)
and recorded `run_attempts.workspace_capture`; grading starts from those two rows:

    capture too_large | incomplete | invalid | persist_failed -> ERROR capture_<value>
    row with storage_key -> ArtifactStore.read (size + SHA-256 verified);
        missing file -> ERROR artifact_missing, wrong bytes -> ERROR artifact_corrupt
    row with content (Phase 4)  -> those bytes
    no row at all               -> ERROR capture_missing
    (a spec that needs no workspace never looks: not_needed grades without one)

Messages carry only the reason's name where they can (D11).

D11: raw ExecResults live only inside the check loop. Logs carry ids, status,
category and exception TYPES - never check names, paths, hidden content or
process output. The internal columns (checks, failure_detail, error_message)
are only ever projected outward through public.public_view().
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

import docker
import docker.errors
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.limits import AgentLimits
from agenteval_core.artifacts import ArtifactCorrupt, ArtifactMissing, ArtifactStore
from agenteval_core.evaluation.checks import (
    evaluator_files,
    execution_order,
    file_paths_needed,
    judge,
    judge_static,
    plan,
)
from agenteval_core.evaluation.classify import Verdict, classify, platform_category
from agenteval_core.evaluation.integrity import assess, baseline
from agenteval_core.evaluation.metrics import TrajectoryMetrics, compute
from agenteval_core.evaluation.results import AttemptFacts, CheckResult, IntegrityResult
from agenteval_core.evaluation.scoring import (
    DEFAULT_SCORING_CONFIG,
    ScoreResult,
    ScoringConfig,
    components,
    score,
)
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.evaluation.taxonomy import EvaluationStatus, FailureCategory
from agenteval_core.logging import get_logger
from agenteval_core.models import (
    AgentVersion,
    Evaluation,
    Run,
    RunAttempt,
    Task,
    TrajectoryEvent,
)
from agenteval_core.observability import metrics as prom
from agenteval_core.reliability.backoff import backoff_delay
from agenteval_core.reliability.policy import after_evaluation_error, after_evaluation_shutdown
from agenteval_core.reliability.taxonomy import (
    EvaluationErrorReason,
    FailureReason,
    WorkspaceCapture,
)
from agenteval_core.repositories.evaluations import ClaimedEvaluation, EvaluationRepository
from agenteval_core.repositories.trajectory import TrajectoryRepository
from agenteval_core.states import AttemptStatus, Outcome
from agenteval_core.tools.workspace import WorkspaceUnavailable as ToolWorkspaceUnavailable
from agenteval_worker.sandbox.evaluation import EvaluationSession, WorkspaceSnapshot
from agenteval_worker.sandbox.session import ContainerSpec, SandboxError
from agenteval_worker.sandbox.workspace import EvaluationWorkspace

if TYPE_CHECKING:
    from agenteval_worker.lease_manager import LeaseManager

log = get_logger(__name__)

# Container create/start, /eval install and the first restore, on top of the
# spec's own timeout_s (which already bounds the sum of per-check timeouts).
SETUP_GRACE_S = 60
EVENT_PAGE = 1000


# A capture that left no usable snapshot: grading it again cannot change that.
_CAPTURE_FAILURES: dict[WorkspaceCapture, EvaluationErrorReason] = {
    WorkspaceCapture.TOO_LARGE: EvaluationErrorReason.CAPTURE_TOO_LARGE,
    WorkspaceCapture.INCOMPLETE: EvaluationErrorReason.CAPTURE_INCOMPLETE,
    WorkspaceCapture.INVALID: EvaluationErrorReason.CAPTURE_INVALID,
    WorkspaceCapture.PERSIST_FAILED: EvaluationErrorReason.CAPTURE_PERSIST_FAILED,
}


# What a drive of the evaluation container can raise when the container (not the
# grading logic) is at fault: retried, since a fresh container may well work.
_SANDBOX_ERRORS = (SandboxError, ToolWorkspaceUnavailable, docker.errors.DockerException)


class WorkspaceUnavailable(Exception):
    """The attempt's workspace can't be loaded: ERROR with this reason (A2,
    never the candidate's fault). The message is D11-safe: reason name only."""

    def __init__(self, reason: EvaluationErrorReason) -> None:
        super().__init__(f"workspace capture failed: {reason.value}")
        self.reason = reason


def parse_spec(raw: Mapping[str, Any] | None) -> EvaluationSpec | None:
    """The task's stored spec, or None if it has none. Raises ValidationError."""
    return None if raw is None else EvaluationSpec.model_validate(raw)


def needs_workspace(spec: EvaluationSpec) -> bool:
    """A6: only specs with executing checks, file checks or protected paths
    need the agent's workspace - and therefore a snapshot and a container.
    Final-answer-only specs are graded from the attempt row alone."""
    return bool(spec.protected_paths) or any(c.kind != "final_answer" for c in spec.checks)


@dataclass(frozen=True)
class Graded:
    checks: list[CheckResult]  # spec order: public_view() positions rely on it
    verdict: Verdict
    score: ScoreResult | None  # only for SUCCEEDED


def grade(
    spec: EvaluationSpec,
    facts: AttemptFacts,
    results: Mapping[str, CheckResult],
    integrity: IntegrityResult,
    metrics: TrajectoryMetrics,
    config: ScoringConfig,
) -> Graded:
    """Pure: put check results back in spec order, classify, and score a
    graded verdict. Every spec check must have a result."""
    checks = [results[c.name] for c in spec.checks]
    verdict = classify(facts, has_spec=True, checks=checks, integrity=integrity, metrics=metrics)
    scored = None
    if verdict.status is EvaluationStatus.SUCCEEDED:
        scored = score(components(verdict, checks, metrics, facts), config, integrity=integrity)
    return Graded(checks, verdict, scored)


class EvaluationFailed(Exception):
    """The evaluator could not finish; becomes ERROR / evaluation_error (or a
    retry), with the structured reason chosen where it is raised."""

    def __init__(
        self,
        message: str,
        reason: EvaluationErrorReason = EvaluationErrorReason.INTERNAL_UNEXPECTED,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class Recorded(NamedTuple):
    """What `record` created: the row's id and the status it was created with."""

    evaluation_id: uuid.UUID
    status: EvaluationStatus


@dataclass(frozen=True)
class _Loaded:
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    task: Task
    facts: AttemptFacts
    metrics: TrajectoryMetrics
    config: ScoringConfig | None  # None: the batch's stored config is invalid
    capture: str | None  # run_attempts.workspace_capture
    max_eval_attempts: int  # the batch's, or the ad hoc setting (F9)


@dataclass(frozen=True)
class _Done:
    graded: Graded
    integrity: IntegrityResult
    config: ScoringConfig


@dataclass(frozen=True)
class _Failed:
    reason: EvaluationErrorReason
    message: str


class _Slot:
    """Per-grading state the lease callback (running on the event loop, not in
    `grade`'s call stack) needs: the work to cancel, and whether the lease may
    still be considered lost (not after the finish write began)."""

    __slots__ = ("work", "lost", "finishing")

    def __init__(self) -> None:
        self.work: asyncio.Future[_Done | _Failed] | None = None
        self.lost = False
        self.finishing = False


class EvaluationRunner:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        docker_client: docker.DockerClient,
        artifact_store: ArtifactStore,
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
        setup_grace_s: float = SETUP_GRACE_S,
        worker_id: str = "evaluator",
        eval_max_attempts: int = 1,
        lease_manager: LeaseManager | None = None,
        lease_ttl_s: float = 30.0,
        kill_by_attempt_label: Callable[[str], int] | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._artifacts = artifact_store
        self._docker = docker_client
        self._max_output = max_output_bytes
        self._workspace_mb = workspace_tmpfs_mb
        self._pids_limit = pids_limit
        self._setup_grace_s = setup_grace_s
        self._worker_id = worker_id
        # F9: a direct construction keeps Phase 4's single try; main() passes the setting.
        self._eval_max_attempts = eval_max_attempts
        self._lease_manager = lease_manager
        self._lease_ttl_s = lease_ttl_s
        self._kill_by_attempt_label = kill_by_attempt_label
        self._background: set[asyncio.Task[Any]] = set()

    # -- the fast path -------------------------------------------------------------------

    async def evaluate(self, task: Task, attempt: RunAttempt) -> uuid.UUID | None:
        """The executor's fast path: record, then grade inline. Returns the
        evaluation id, or None if another evaluator already owns it. Evaluator
        failures end as ERROR rows (or a PENDING retry the reconciler
        dispatches); only infrastructure faults (the database) raise. `task` is
        unused: record() loads it, as the reconciler's A1 recovery must."""
        recorded = await self.record(attempt)
        if recorded is None:
            return None
        if recorded.status is not EvaluationStatus.PENDING:
            return recorded.evaluation_id
        return await self.grade(recorded.evaluation_id)

    # -- record: tx A --------------------------------------------------------------------

    async def record(self, attempt: RunAttempt) -> Recorded | None:
        return await self.record_attempt(attempt.id)

    async def record_attempt(self, attempt_id: uuid.UUID) -> Recorded | None:
        """E1 / E2 / E3 for a FINISHED attempt (F11), by id so the reconciler's
        A1 recovery can call it. Returns None if the attempt isn't finished or
        already has its row (a duplicate recorder stops here)."""
        loaded = await self._load(attempt_id)
        if loaded is None:
            return None
        facts, metrics, budget = loaded.facts, loaded.metrics, loaded.max_eval_attempts
        base: dict[str, Any] = {"metrics": metrics.model_dump(mode="json")}

        try:
            spec = parse_spec(loaded.task.evaluation)
        except ValidationError as exc:
            return await self._create_error(
                attempt_id,
                f"invalid evaluation spec: {exc}",
                base,
                EvaluationErrorReason.SPEC_INVALID,
                budget,
            )

        if spec is None or platform_category(facts) is not None:
            verdict = classify(facts, has_spec=spec is not None, metrics=metrics)
            return await self._create_finished(attempt_id, verdict, base, budget)

        if loaded.config is None:
            return await self._create_error(
                attempt_id,
                "invalid batch scoring config",
                base,
                EvaluationErrorReason.SCORING_CONFIG_INVALID,
                budget,
            )
        if needs_workspace(spec):
            failure = await self._capture_failure(loaded)
            if failure is not None:
                return await self._create_error(
                    attempt_id, str(WorkspaceUnavailable(failure)), base, failure, budget
                )

        # E1: the PENDING row.
        async with self._sm() as s:
            evaluation_id = await EvaluationRepository(s).create(
                attempt_id, EvaluationStatus.PENDING, max_eval_attempts=budget, **base
            )
            if evaluation_id is None:
                await s.rollback()
                log.info("evaluation_exists")
                return None
            await s.commit()
        prom.evaluation_transition("E1")
        return Recorded(evaluation_id, EvaluationStatus.PENDING)

    # -- grade: claim, check, finish -----------------------------------------------------

    async def grade(
        self,
        evaluation_id: uuid.UUID,
        *,
        acknowledge: Callable[[], Awaitable[None]] | None = None,
    ) -> uuid.UUID | None:
        """E4 claim -> grade -> finish. Returns the id if this call claimed the
        row, None if it did not (not PENDING any more, or its backoff has not
        elapsed): a duplicate or stale dispatch is harmless. `acknowledge` is
        the stream message's ack: called once the claim has been decided, won or
        not, and never if the claim itself failed (the message stays pending and
        the reconciler's reclaim and dispatch cover it)."""
        t_sent = time.monotonic()  # seeds the lease's self-stop deadline, like a run's
        async with self._sm() as s:
            claimed = await EvaluationRepository(s).claim(
                evaluation_id, self._worker_id, ttl_s=self._lease_ttl_s
            )
            if claimed is None:
                await s.rollback()
            else:
                await s.commit()
        if acknowledge is not None:
            try:
                await acknowledge()
            except Exception as exc:
                # The claim is already durable: a failed XACK must not abandon the
                # grading. The entry stays pending and the reconciler reclaims it.
                log.warning("evaluation_ack_failed", error_type=type(exc).__name__)
        if claimed is None:
            log.info("evaluation_claim_lost")
            return None
        prom.evaluation_transition("E4")
        log.info(
            "evaluation_claimed",
            eval_attempts=claimed.eval_attempts,
            max_eval_attempts=claimed.max_eval_attempts,
        )

        slot = _Slot()
        if self._lease_manager is not None:
            self._lease_manager.hold(
                "evaluation",
                str(claimed.id),
                claimed.lease_epoch,
                t_sent,
                on_lease_lost=lambda: self._on_lease_lost(slot, claimed),
            )
        try:
            await self._grade_claimed(claimed, slot)
        finally:
            # Right after the finish write: a renewal after it would match 0 rows,
            # look like a lost lease and kill containers for nothing.
            if self._lease_manager is not None:
                self._lease_manager.release("evaluation", str(claimed.id), claimed.lease_epoch)
        return claimed.id

    def _on_lease_lost(self, slot: _Slot, claimed: ClaimedEvaluation) -> None:
        """Called from the lease manager's own tasks. The reconciler owns the row
        now: stop the work, kill the container by its attempt label (best effort;
        `_check`'s own cleanup usually got there first) and let `_grade_claimed`
        write nothing."""
        if slot.finishing:
            return  # a beat that raced the finish: nothing left to stop
        slot.lost = True
        log.warning("evaluation_lease_lost", evaluation_id=str(claimed.id))
        if slot.work is not None and not slot.work.done():
            slot.work.cancel()
        if self._kill_by_attempt_label is not None:
            task = asyncio.create_task(
                asyncio.to_thread(self._kill_by_attempt_label, str(claimed.attempt_id))
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _grade_claimed(self, claimed: ClaimedEvaluation, slot: _Slot) -> None:
        started = time.monotonic()
        slot.work = asyncio.ensure_future(self._work(claimed))
        try:
            outcome = await slot.work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            outer_cancelled = current is not None and current.cancelling() > 0
            if slot.lost:
                log.warning("evaluation_lease_lost_skip_persist", evaluation_id=str(claimed.id))
                if outer_cancelled:
                    raise
                return
            # Shutdown hard-cancelled us (the work, cancelled with us, has already
            # cleaned its container up): record why, best effort, then go.
            await asyncio.shield(self._release_for_shutdown(claimed, started))
            raise
        if slot.lost:
            log.warning("evaluation_lease_lost_skip_persist", evaluation_id=str(claimed.id))
            return
        slot.finishing = True
        if (
            isinstance(outcome, _Done)
            and outcome.graded.verdict.status is EvaluationStatus.SUCCEEDED
        ):
            await self._finish_succeeded(claimed, outcome, started)
        elif isinstance(outcome, _Done):
            v = outcome.graded.verdict
            reason = (
                EvaluationErrorReason.CHECK_ERROR
                if v.status is EvaluationStatus.ERROR
                else EvaluationErrorReason.INTERNAL_UNEXPECTED
            )
            fields = {
                "checks": [c.model_dump(mode="json") for c in outcome.graded.checks],
                "integrity": outcome.integrity.model_dump(mode="json"),
                **_verdict_fields(v),
                "error_message": v.detail,
                "duration_ms": _ms_since(started),
            }
            await self._finish_failed(claimed, reason, v.detail, fields)
        else:
            await self._finish_failed(
                claimed,
                outcome.reason,
                outcome.message,
                {
                    "failure_category": FailureCategory.EVALUATION_ERROR,
                    "failure_tags": [],
                    "error_message": outcome.message,
                    "duration_ms": _ms_since(started),
                },
            )

    async def _work(self, claimed: ClaimedEvaluation) -> _Done | _Failed:
        """Load everything, run the checks and classify. Every failure becomes a
        `_Failed` with a structured reason; only cancellation propagates."""
        try:
            loaded = await self._load(claimed.attempt_id)
            if loaded is None:
                return _Failed(
                    EvaluationErrorReason.INTERNAL_UNEXPECTED, "the attempt is not finished"
                )
            try:
                spec = parse_spec(loaded.task.evaluation)
            except ValidationError as exc:
                return _Failed(
                    EvaluationErrorReason.SPEC_INVALID, f"invalid evaluation spec: {exc}"
                )
            if spec is None or platform_category(loaded.facts) is not None:
                return _Failed(
                    EvaluationErrorReason.INTERNAL_UNEXPECTED, "the attempt needs no grading"
                )
            if loaded.config is None:
                return _Failed(
                    EvaluationErrorReason.SCORING_CONFIG_INVALID, "invalid batch scoring config"
                )
            snapshot = None
            if needs_workspace(spec):
                try:
                    snapshot = await self._load_workspace(loaded)
                except WorkspaceUnavailable as exc:
                    return _Failed(exc.reason, str(exc))
            results, integrity = await self._check(loaded.task, spec, snapshot, loaded)
            graded = grade(spec, loaded.facts, results, integrity, loaded.metrics, loaded.config)
            return _Done(graded, integrity, loaded.config)
        except EvaluationFailed as exc:
            return _Failed(exc.reason, str(exc))
        except Exception as exc:
            log.exception("evaluator_error", error_type=type(exc).__name__)
            return _Failed(
                EvaluationErrorReason.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
            )

    # -- grading -------------------------------------------------------------------------

    async def _check(
        self,
        task: Task,
        spec: EvaluationSpec,
        snapshot: WorkspaceSnapshot | None,
        loaded: _Loaded,
    ) -> tuple[dict[str, CheckResult], IntegrityResult]:
        facts = loaded.facts
        static, executing = execution_order(spec)
        results: dict[str, CheckResult] = {}
        if snapshot is None:  # A6: nothing needs the workspace
            for c in static:
                results[c.name] = judge_static(c, final_answer=facts.final_answer, evidence={})
            return results, assess({}, {})

        budget = spec.timeout_s + self._setup_grace_s
        opening = asyncio.ensure_future(
            asyncio.to_thread(
                EvaluationSession.open,
                self._docker,
                ContainerSpec(
                    image=task.image,
                    lifetime_s=int(budget) + 60,  # sweep_orphans backstop
                    memory_mb=task.memory_mb,
                    cpu_limit=task.cpu_limit,
                    labels={
                        "agenteval.run_id": str(loaded.run_id),
                        "agenteval.attempt_id": str(loaded.attempt_id),
                    },
                ),
                snapshot,
                {**spec.hidden_files, **evaluator_files(spec)},
                max_output_bytes=self._max_output,
                workspace_tmpfs_mb=self._workspace_mb,
                pids_limit=self._pids_limit,
            )
        )
        session: EvaluationSession | None = None
        try:
            async with asyncio.timeout(budget):
                session = await asyncio.shield(opening)
                ws = EvaluationWorkspace(session)
                evidence = await ws.collect_evidence(file_paths_needed(spec))
                integrity = assess(
                    baseline(task.files, spec.protected_paths),
                    evidence,
                    normalized_paths=snapshot.normalized_paths,
                )
                for c in static:  # C5: static checks first
                    results[c.name] = judge_static(
                        c, final_answer=facts.final_answer, evidence=evidence
                    )
                for c in executing:  # each on a freshly restored workspace
                    results[c.name] = judge(c, await ws.run_plan(plan(c)))
            return results, integrity
        except TimeoutError as exc:
            raise EvaluationFailed(
                f"evaluation did not finish within {budget:g}s",
                EvaluationErrorReason.BUDGET_TIMEOUT,
            ) from exc
        except _SANDBOX_ERRORS as exc:
            # SandboxError/SnapshotError from open(), WorkspaceUnavailable from a
            # drive, or Docker itself: the container, not the grading logic.
            raise EvaluationFailed(
                f"evaluation failed: {type(exc).__name__}: {exc}",
                EvaluationErrorReason.SANDBOX_FAILED,
            ) from exc
        except Exception as exc:
            raise EvaluationFailed(
                f"evaluation failed: {type(exc).__name__}: {exc}",
                EvaluationErrorReason.INTERNAL_UNEXPECTED,
            ) from exc
        finally:
            await asyncio.shield(self._close(opening, session))

    @staticmethod
    async def _close(
        opening: asyncio.Future[EvaluationSession], session: EvaluationSession | None
    ) -> None:
        """Always remove the evaluation container: on success, error,
        timeout and cancellation. If open() is still running, wait for it
        (it has its own deadlines) and remove what it created."""
        if session is None:
            try:
                session = await opening
            except Exception:
                return  # open() removes its own container on failure
        # remove(force=True) also ends any exec still streaming in a thread.
        await asyncio.to_thread(session.close)

    async def _capture_failure(self, loaded: _Loaded) -> EvaluationErrorReason | None:
        """The reason the attempt has no usable snapshot, decided from rows alone
        (no file read): a failed capture, or no artifact row at all. None when a
        row exists; whether its file is intact is `_load_workspace`'s to find out."""
        if loaded.capture is not None:
            failure = _CAPTURE_FAILURES.get(WorkspaceCapture(loaded.capture))
            if failure is not None:
                return failure
        async with self._sm() as s:
            row = await EvaluationRepository(s).get_artifact(loaded.attempt_id)
        return EvaluationErrorReason.CAPTURE_MISSING if row is None else None

    async def _load_workspace(self, loaded: _Loaded) -> WorkspaceSnapshot:
        """The snapshot the attempt's executor saved (ADR-008 A): by its row,
        verified. Raises WorkspaceUnavailable with the reason otherwise."""
        failure = await self._capture_failure(loaded)
        if failure is not None:
            raise WorkspaceUnavailable(failure)
        async with self._sm() as s:
            row = await EvaluationRepository(s).get_artifact(loaded.attempt_id)
            if row is None:
                raise WorkspaceUnavailable(EvaluationErrorReason.CAPTURE_MISSING)
            key, size, sha, content = row.storage_key, row.size_bytes, row.sha256, row.content
            raw_paths = row.artifact_metadata.get("normalized_paths", [])
        normalized = tuple(str(p) for p in raw_paths)
        if key is None:  # a Phase 4 row: the bytes are inline
            assert content is not None  # ck_attempt_artifacts_one_location
            return WorkspaceSnapshot(content, sha, normalized)
        try:
            data = await self._artifacts.read(key, size, sha)
        except ArtifactMissing:
            log.warning("artifact_missing", attempt_id=str(loaded.attempt_id))
            raise WorkspaceUnavailable(EvaluationErrorReason.ARTIFACT_MISSING) from None
        except ArtifactCorrupt:
            log.warning("artifact_corrupt", attempt_id=str(loaded.attempt_id))
            raise WorkspaceUnavailable(EvaluationErrorReason.ARTIFACT_CORRUPT) from None
        return WorkspaceSnapshot(data, sha, normalized)

    # -- loading -------------------------------------------------------------------------

    async def _load(self, attempt_id: uuid.UUID) -> _Loaded | None:
        """The attempt, its task, trajectory and scoring config, all from
        Postgres: nothing is handed over from the executor (that is what lets
        another worker grade it)."""
        async with self._sm() as s:
            row = await s.get(RunAttempt, attempt_id, populate_existing=True)
            if row is None or row.status != AttemptStatus.FINISHED or row.outcome is None:
                log.warning("evaluation_attempt_not_finished")
                return None
            run = await s.get(Run, row.run_id)
            assert run is not None
            task = await s.get(Task, run.task_id)
            assert task is not None
            version = (
                await s.get(AgentVersion, run.agent_version_id) if run.agent_version_id else None
            )
            config: ScoringConfig | None = DEFAULT_SCORING_CONFIG
            budget = self._eval_max_attempts
            if run.evaluation_id is not None:
                batch = await s.get(Evaluation, run.evaluation_id)
                assert batch is not None
                budget = batch.eval_max_attempts  # F9, G1: pinned by the batch
                try:
                    config = ScoringConfig.model_validate(batch.scoring_config)
                except ValidationError:
                    config = None
            events: list[TrajectoryEvent] = []
            repo = TrajectoryRepository(s)
            after = -1
            while page := await repo.list_for_attempt(row.id, after_seq=after, limit=EVENT_PAGE):
                events += page
                after = page[-1].seq
        limits = _limits(version)
        facts = AttemptFacts(
            outcome=Outcome(row.outcome),
            error_message=row.error_message,
            final_answer=row.final_answer,
            steps=row.steps,
            cost_usd=row.cost_usd,
            max_steps=limits.max_steps if limits else None,
            max_cost_usd=limits.max_cost_usd if limits else None,
            failure_reason=FailureReason(row.failure_reason) if row.failure_reason else None,
        )
        return _Loaded(
            row.id, row.run_id, task, facts, compute(events), config, row.workspace_capture, budget
        )

    # -- persistence ---------------------------------------------------------------------

    async def _create_finished(
        self, attempt_id: uuid.UUID, verdict: Verdict, base: dict[str, Any], budget: int
    ) -> Recorded | None:
        """E2: SKIPPED, created finished."""
        async with self._sm() as s:
            evaluation_id = await EvaluationRepository(s).create(
                attempt_id,
                verdict.status,
                max_eval_attempts=budget,
                **base,
                **_verdict_fields(verdict),
                duration_ms=0,
            )
            await s.commit()
        if evaluation_id is None:
            return None
        prom.evaluation_transition("E2")
        log.info("evaluation_finished", status=verdict.status, category=verdict.category)
        return Recorded(evaluation_id, verdict.status)

    async def _create_error(
        self,
        attempt_id: uuid.UUID,
        message: str,
        base: dict[str, Any],
        reason: EvaluationErrorReason,
        budget: int,
    ) -> Recorded | None:
        """E3: a non-retryable ERROR created finished, with its reason."""
        async with self._sm() as s:
            evaluation_id = await EvaluationRepository(s).create(
                attempt_id,
                EvaluationStatus.ERROR,
                max_eval_attempts=budget,
                **base,
                error_reason=reason.value,
                failure_category=FailureCategory.EVALUATION_ERROR,
                failure_tags=[],
                error_message=message,
                duration_ms=0,
            )
            await s.commit()
        if evaluation_id is None:
            return None
        prom.evaluation_transition("E3")
        log.warning("evaluation_finished", status=EvaluationStatus.ERROR, error_reason=reason)
        return Recorded(evaluation_id, EvaluationStatus.ERROR)

    async def _finish_failed(
        self,
        claimed: ClaimedEvaluation,
        reason: EvaluationErrorReason,
        message: str,
        fields: dict[str, Any],
    ) -> None:
        """E6 (ERROR) or E7 (PENDING with backoff), fenced by the claim's epoch.
        `fields` are the ERROR columns; a retry writes none of them."""
        decision = after_evaluation_error(
            reason,
            eval_attempts=claimed.eval_attempts,
            max_eval_attempts=claimed.max_eval_attempts,
        )
        async with self._sm() as s:
            repo = EvaluationRepository(s)
            if decision.status == EvaluationStatus.PENDING:  # E7
                applied = await repo.release(
                    claimed.id,
                    claimed.lease_epoch,
                    error_reason=reason.value,
                    retry_in_s=backoff_delay(claimed.eval_attempts),
                )
            else:  # E6
                assert decision.error_kind is not None
                applied = await repo.finish(
                    claimed.id,
                    claimed.lease_epoch,
                    EvaluationStatus.ERROR,
                    error_reason=reason.value,
                    error_kind=decision.error_kind.value,
                    **fields,
                )
            if not applied:
                await s.rollback()
                log.warning("evaluation_finish_lost", transition=decision.code)
                return
            await s.commit()
        prom.evaluation_transition(decision.code)
        prom.evaluation_duration_ms(decision.status, fields.get("duration_ms"))
        log.warning(
            "evaluation_finished",
            status=decision.status,
            transition=decision.code,
            error_reason=reason,
            eval_attempts=claimed.eval_attempts,
        )

    async def _release_for_shutdown(self, claimed: ClaimedEvaluation, started: float) -> None:
        """E8a / E8b: the worker is going away mid-grading. Best effort: if this
        write is lost too, the lease expires and the reconciler (E9) decides."""
        decision = after_evaluation_shutdown(
            eval_attempts=claimed.eval_attempts, max_eval_attempts=claimed.max_eval_attempts
        )
        reason = EvaluationErrorReason.WORKER_SHUTDOWN
        try:
            async with self._sm() as s:
                repo = EvaluationRepository(s)
                if decision.status == EvaluationStatus.PENDING:  # E8a: due at once
                    applied = await repo.release(
                        claimed.id, claimed.lease_epoch, error_reason=reason.value, retry_in_s=0
                    )
                else:  # E8b
                    assert decision.error_kind is not None
                    applied = await repo.finish(
                        claimed.id,
                        claimed.lease_epoch,
                        EvaluationStatus.ERROR,
                        error_reason=reason.value,
                        error_kind=decision.error_kind.value,
                        failure_category=FailureCategory.EVALUATION_ERROR,
                        failure_tags=[],
                        error_message="interrupted: worker_shutdown",
                        duration_ms=_ms_since(started),
                    )
                if not applied:
                    await s.rollback()
                    return
                await s.commit()
            prom.evaluation_transition(decision.code)
            prom.evaluation_duration_ms(decision.status, _ms_since(started))
            log.warning("evaluation_finished", status=decision.status, transition=decision.code)
        except Exception:
            log.exception("evaluation_shutdown_release_failed")

    async def _finish_succeeded(
        self, claimed: ClaimedEvaluation, done: _Done, started: float
    ) -> None:
        """E5: the terminal state and its score, together, fenced by epoch."""
        graded, v = done.graded, done.graded.verdict
        fields: dict[str, Any] = {
            "checks": [c.model_dump(mode="json") for c in graded.checks],
            "integrity": done.integrity.model_dump(mode="json"),
            **_verdict_fields(v),
            "duration_ms": _ms_since(started),
        }
        async with self._sm() as s:
            repo = EvaluationRepository(s)
            if not await repo.finish(
                claimed.id, claimed.lease_epoch, EvaluationStatus.SUCCEEDED, **fields
            ):
                await s.rollback()  # fenced: a stale grader writes no score either
                log.warning("evaluation_finish_lost", transition="E5")
                return
            if graded.score is not None:
                await repo.add_score(
                    claimed.id,
                    scoring_config_hash=graded.score.scoring_config_hash,
                    scoring_config=done.config.model_dump(mode="json"),
                    components=graded.score.components.model_dump(mode="json"),
                    overall=graded.score.overall,
                )
            await s.commit()
        prom.evaluation_transition("E5")
        prom.evaluation_duration_ms(EvaluationStatus.SUCCEEDED, fields.get("duration_ms"))
        log.info(
            "evaluation_finished",
            status=v.status,
            category=v.category,
            overall=str(graded.score.overall) if graded.score else None,
        )


def _verdict_fields(v: Verdict) -> dict[str, Any]:
    return {
        "task_success": v.task_success,
        "failure_category": v.category,
        "failure_tags": list(v.tags),
        "failure_detail": v.detail,
    }


def _limits(version: AgentVersion | None) -> AgentLimits | None:
    if version is None:
        return None
    try:
        return AgentLimits.model_validate(version.limits)
    except ValidationError:
        return None


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
