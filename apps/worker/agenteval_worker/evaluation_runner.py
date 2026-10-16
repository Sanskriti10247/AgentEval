"""EvaluationRunner: grades one finished agent attempt (Phase 4 step 5).

Runs only after finish_attempt has COMMITTED (D1, A1), in its own
transactions, so evaluation can never roll back or alter the attempt's
outcome. Evaluation never writes runs, run_attempts or trajectory_events.

    tx A  record   attempt_artifacts (the snapshot) + attempt_evaluations as
                   SKIPPED   platform failure (never fairly tested) or no spec
                   ERROR     invalid spec, or the workspace couldn't be captured (A2)
                   PENDING   otherwise
                   (ON CONFLICT DO NOTHING: a duplicate evaluator stops here)
    tx B  claim    PENDING -> RUNNING (exactly one winner)
          grade    fresh EvaluationSession (only if the spec needs the workspace,
                   A6): evidence from the pristine snapshot, static checks, then
                   Step 3 ExecPlans through EvaluationWorkspace; classify, score
    tx C  finish   RUNNING -> SUCCEEDED (+ one scores row) | ERROR

Failure attribution: anything the evaluator itself could not do - capture the
workspace, open/drive the container, finish within its budget - is ERROR /
evaluation_error, never a candidate failure. A check that runs past its OWN
timeout is the candidate's (judge() says tests_failed).

Known crash gap (A1, for the recovery phase): if the worker dies between the
finish_attempt commit and tx A, the attempt has no evaluation row and the
in-memory snapshot is lost; if it dies after tx A/B, the row stays PENDING or
RUNNING. Both are detectable by query; nothing recovers them yet.

D11: raw ExecResults live only inside the check loop. Logs carry ids, status,
category and exception TYPES - never check names, paths, hidden content or
process output. The internal columns (checks, failure_detail, error_message)
are only ever projected outward through public.public_view().
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import docker
import docker.errors
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.limits import AgentLimits
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
from agenteval_core.models import AgentVersion, Evaluation, Run, RunAttempt, Task, TrajectoryEvent
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.repositories.evaluations import EvaluationRepository
from agenteval_core.repositories.trajectory import TrajectoryRepository
from agenteval_core.states import AttemptStatus, Outcome
from agenteval_worker.sandbox.evaluation import EvaluationSession, WorkspaceSnapshot
from agenteval_worker.sandbox.session import ContainerSpec
from agenteval_worker.sandbox.workspace import EvaluationWorkspace

log = get_logger(__name__)

# Container create/start, /eval install and the first restore, on top of the
# spec's own timeout_s (which already bounds the sum of per-check timeouts).
SETUP_GRACE_S = 60
EVENT_PAGE = 1000


@dataclass(frozen=True)
class WorkspaceCapture:
    """What AgentRunner captured from the agent's container before closing
    it: the snapshot, or why it couldn't be taken (A2: an evaluator failure)."""

    snapshot: WorkspaceSnapshot | None = None
    error: str | None = None


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
    """The evaluator could not finish; becomes ERROR / evaluation_error."""


@dataclass(frozen=True)
class _Loaded:
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    facts: AttemptFacts
    metrics: TrajectoryMetrics
    config: ScoringConfig | None  # None: the batch's stored config is invalid


class EvaluationRunner:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        docker_client: docker.DockerClient,
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
        setup_grace_s: float = SETUP_GRACE_S,
    ) -> None:
        self._sm = sessionmaker
        self._docker = docker_client
        self._max_output = max_output_bytes
        self._workspace_mb = workspace_tmpfs_mb
        self._pids_limit = pids_limit
        self._setup_grace_s = setup_grace_s

    async def evaluate(
        self, task: Task, attempt: RunAttempt, workspace: WorkspaceCapture | None
    ) -> uuid.UUID | None:
        """Evaluate a FINISHED agent attempt. Returns the evaluation id, or
        None if another evaluator already owns it. Evaluator failures end as
        ERROR rows; only infrastructure faults (the database) raise."""
        loaded = await self._load(task, attempt)
        if loaded is None:
            return None
        facts, metrics = loaded.facts, loaded.metrics
        base = {"metrics": metrics.model_dump(mode="json")}

        try:
            spec = parse_spec(task.evaluation)
        except ValidationError as exc:
            return await self._create_error(attempt, f"invalid evaluation spec: {exc}", base)

        if spec is None or platform_category(facts) is not None:
            verdict = classify(facts, has_spec=spec is not None, metrics=metrics)
            return await self._create_finished(attempt, verdict, base)

        if loaded.config is None:
            return await self._create_error(attempt, "invalid batch scoring config", base)
        snapshot = None
        if needs_workspace(spec):
            if workspace is None or workspace.snapshot is None:
                why = workspace.error if workspace and workspace.error else "no snapshot captured"
                return await self._create_error(attempt, f"workspace capture failed: {why}", base)
            snapshot = workspace.snapshot

        # tx A: the snapshot and a PENDING row, together.
        async with self._sm() as s:
            repo = EvaluationRepository(s)
            if snapshot is not None:
                await repo.add_artifact(attempt.id, snapshot.data, snapshot.sha256)
            evaluation_id = await repo.create(attempt.id, EvaluationStatus.PENDING, **base)
            if evaluation_id is None:
                await s.rollback()
                log.info("evaluation_exists")
                return None
            await s.commit()
        return await self._run(evaluation_id, task, spec, snapshot, loaded)

    async def _run(
        self,
        evaluation_id: uuid.UUID,
        task: Task,
        spec: EvaluationSpec,
        snapshot: WorkspaceSnapshot | None,
        loaded: _Loaded,
    ) -> uuid.UUID | None:
        assert loaded.config is not None
        config = loaded.config
        # tx B: exactly one evaluator grades.
        async with self._sm() as s:
            if not await EvaluationRepository(s).claim(evaluation_id):
                await s.rollback()
                log.info("evaluation_claim_lost")
                return None
            await s.commit()

        started = time.monotonic()
        try:
            results, integrity = await self._check(task, spec, snapshot, loaded)
            graded = grade(spec, loaded.facts, results, integrity, loaded.metrics, config)
        except asyncio.CancelledError:
            # Shutdown hard-cancelled us: record why (best effort), then go.
            await asyncio.shield(
                self._finish_error(evaluation_id, "interrupted: worker_shutdown", started)
            )
            raise
        except EvaluationFailed as exc:
            await self._finish_error(evaluation_id, str(exc), started)
            return evaluation_id
        except Exception as exc:
            log.exception("evaluator_error", error_type=type(exc).__name__)
            await self._finish_error(evaluation_id, f"{type(exc).__name__}: {exc}", started)
            return evaluation_id
        await self._finish_graded(evaluation_id, graded, integrity, config, started)
        return evaluation_id

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
            raise EvaluationFailed(f"evaluation did not finish within {budget:g}s") from exc
        except Exception as exc:
            # WorkspaceUnavailable, SandboxError/SnapshotError from open(),
            # Docker errors, or missing evidence for a protected path.
            raise EvaluationFailed(f"evaluation failed: {type(exc).__name__}: {exc}") from exc
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

    # -- loading -------------------------------------------------------------------------

    async def _load(self, task: Task, attempt: RunAttempt) -> _Loaded | None:
        async with self._sm() as s:
            row = await s.get(RunAttempt, attempt.id, populate_existing=True)
            if row is None or row.status != AttemptStatus.FINISHED or row.outcome is None:
                log.warning("evaluation_attempt_not_finished")
                return None
            run = await s.get(Run, row.run_id)
            assert run is not None
            version = (
                await s.get(AgentVersion, run.agent_version_id) if run.agent_version_id else None
            )
            config: ScoringConfig | None = DEFAULT_SCORING_CONFIG
            if run.evaluation_id is not None:
                batch = await s.get(Evaluation, run.evaluation_id)
                assert batch is not None
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
        return _Loaded(row.id, row.run_id, facts, compute(events), config)

    # -- persistence ---------------------------------------------------------------------

    async def _create_finished(
        self, attempt: RunAttempt, verdict: Verdict, base: dict[str, Any]
    ) -> uuid.UUID | None:
        async with self._sm() as s:
            evaluation_id = await EvaluationRepository(s).create(
                attempt.id, verdict.status, **base, **_verdict_fields(verdict), duration_ms=0
            )
            await s.commit()
        if evaluation_id is not None:
            log.info("evaluation_finished", status=verdict.status, category=verdict.category)
        return evaluation_id

    async def _create_error(
        self, attempt: RunAttempt, message: str, base: dict[str, Any]
    ) -> uuid.UUID | None:
        async with self._sm() as s:
            evaluation_id = await EvaluationRepository(s).create(
                attempt.id,
                EvaluationStatus.ERROR,
                **base,
                failure_category=FailureCategory.EVALUATION_ERROR,
                failure_tags=[],
                error_message=message,
                duration_ms=0,
            )
            await s.commit()
        if evaluation_id is not None:
            log.warning("evaluation_finished", status=EvaluationStatus.ERROR)
        return evaluation_id

    async def _finish_error(self, evaluation_id: uuid.UUID, message: str, started: float) -> None:
        async with self._sm() as s:
            await EvaluationRepository(s).finish(
                evaluation_id,
                EvaluationStatus.ERROR,
                failure_category=FailureCategory.EVALUATION_ERROR,
                failure_tags=[],
                error_message=message,
                duration_ms=_ms_since(started),
            )
            await s.commit()
        log.warning("evaluation_finished", status=EvaluationStatus.ERROR)

    async def _finish_graded(
        self,
        evaluation_id: uuid.UUID,
        graded: Graded,
        integrity: IntegrityResult,
        config: ScoringConfig,
        started: float,
    ) -> None:
        v = graded.verdict
        fields: dict[str, Any] = {
            "checks": [c.model_dump(mode="json") for c in graded.checks],
            "integrity": integrity.model_dump(mode="json"),
            **_verdict_fields(v),
            "duration_ms": _ms_since(started),
        }
        if v.status is EvaluationStatus.ERROR:  # a check could not run
            fields["error_message"] = v.detail
        # tx C: the terminal state and its score, together.
        async with self._sm() as s:
            repo = EvaluationRepository(s)
            if not await repo.finish(evaluation_id, v.status, **fields):
                await s.rollback()
                log.warning("evaluation_finish_lost")
                return
            if graded.score is not None:
                await repo.add_score(
                    evaluation_id,
                    scoring_config_hash=graded.score.scoring_config_hash,
                    scoring_config=config.model_dump(mode="json"),
                    components=graded.score.components.model_dump(mode="json"),
                    overall=graded.score.overall,
                )
            await s.commit()
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
