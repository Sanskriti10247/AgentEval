"""Evaluation batches and their results (Phase 4 step 6).

D11 - the single construction point: _public() is the only code that turns a
stored evaluation into API output, and it returns public_view(...) and
nothing else. Aggregates use closed vocabularies (statuses, categories) and
numbers only. Internal columns (failure_detail, error_message, integrity
details, check names/expected/actual/detail) are never selected for output.

Success-rate semantics are Step 3's success_stats() (D7), unchanged.
"""

import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.evaluation.classify import Verdict, platform_category
from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.public import PublicEvaluation, public_view
from agenteval_core.evaluation.results import AttemptFacts, CheckResult
from agenteval_core.evaluation.scoring import (
    DEFAULT_SCORING_CONFIG,
    PLACES,
    ScoreComponents,
    ScoreResult,
    ScoringConfig,
)
from agenteval_core.evaluation.stats import success_stats
from agenteval_core.evaluation.taxonomy import DatasetStatus, EvaluationStatus, FailureCategory
from agenteval_core.logging import get_logger
from agenteval_core.models import Evaluation, Run
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.agents import AgentVersionRepository
from agenteval_core.repositories.batches import BatchRepository, ResultRecord, TaskMeta
from agenteval_core.repositories.datasets import DatasetRepository
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.schemas.evaluation import (
    MAX_BATCH_RUNS,
    MAX_COMPARE,
    AttemptEvaluationRead,
    Compare,
    CompareEntry,
    CostTotals,
    EvaluationCreate,
    EvaluationRead,
    Latency,
    MeanScore,
    Progress,
    ResultPage,
    ResultRow,
    Summary,
    TaskComparison,
    TaskSummary,
    TokenTotals,
)
from agenteval_core.services.errors import (
    Conflict,
    DispatchUnavailable,
    IdempotencyConflict,
    InvalidEvaluation,
    NotFound,
)
from agenteval_core.states import Outcome, RunStatus

log = get_logger(__name__)

TERMINAL = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})
_UNFINISHED_EVAL = frozenset({EvaluationStatus.PENDING, EvaluationStatus.RUNNING})


@dataclass(frozen=True)
class CreateResult:
    evaluation: EvaluationRead
    created: bool


# -- the D11 projection -----------------------------------------------------------------------


def _public(r: ResultRecord, scoring_config_hash: str) -> PublicEvaluation | None:
    """The ONLY way a stored evaluation leaves the service (D11)."""
    if r.eval_status is None:
        return None
    verdict = Verdict(
        status=EvaluationStatus(r.eval_status),
        task_success=r.task_success,
        category=FailureCategory(r.failure_category) if r.failure_category else None,
        tags=tuple(r.failure_tags or ()),
        detail="",  # internal; public_view never reads it, and we never load it
    )
    score = None
    if r.score_components is not None and r.score_overall is not None:
        score = ScoreResult(
            components=ScoreComponents.model_validate(r.score_components),
            overall=r.score_overall,
            integrity_gate_applied=r.integrity_ok is False,
            scoring_config_hash=scoring_config_hash,
        )
    return public_view(
        verdict,
        [CheckResult.model_validate(c) for c in r.checks or []],
        score=score,
        metrics=TrajectoryMetrics.model_validate(r.metrics) if r.metrics else None,
    )


# -- aggregation (pure) -------------------------------------------------------------------------


def _result_of(r: ResultRecord) -> tuple[EvaluationStatus, FailureCategory | None] | None:
    """(evaluation status, category) for success_stats(), or None while the
    attempt is still in progress (not counted yet). A run that FAILED before
    any attempt has no evaluation row: Step 3's platform_category() decides."""
    if r.eval_status is not None:
        status = EvaluationStatus(r.eval_status)
        if status in _UNFINISHED_EVAL:
            return None
        category = FailureCategory(r.failure_category) if r.failure_category else None
        return status, category
    if r.run_status == RunStatus.FAILED and r.attempt_no is None and r.run_outcome:
        category = platform_category(AttemptFacts(Outcome(r.run_outcome), r.run_error_message))
        if category is not None:
            return EvaluationStatus.SKIPPED, category
    return None


def _nearest_rank(sorted_values: Sequence[int], p: float) -> int:
    return sorted_values[max(0, math.ceil(p * len(sorted_values)) - 1)]


def _mean(values: Iterable[Decimal]) -> Decimal | None:
    vals = list(values)
    return (sum(vals, Decimal(0)) / len(vals)).quantize(PLACES) if vals else None


def _delta(rate: float | None, base: float | None) -> float | None:
    return None if rate is None or base is None else rate - base


def summarize(
    evaluation_id: uuid.UUID,
    records: Sequence[ResultRecord],
    tasks: dict[uuid.UUID, TaskMeta],
) -> Summary:
    finished = [(r, res) for r in records if (res := _result_of(r)) is not None]
    stats = success_stats(res for _, res in finished)
    in_progress = len(records) - len(finished)

    graded = [r for r, (st, _) in finished if st is EvaluationStatus.SUCCEEDED]
    scored = [r for r in graded if r.score_components is not None]
    mean_score = None
    if scored:
        comps = [ScoreComponents.model_validate(r.score_components) for r in scored]
        overall = _mean(r.score_overall for r in scored if r.score_overall is not None)
        task_success = _mean(c.task_success for c in comps)
        efficiency = _mean(c.efficiency for c in comps)
        assert overall is not None and task_success is not None and efficiency is not None
        mean_score = MeanScore(
            overall=overall,
            task_success=task_success,
            test_score=_mean(c.test_score for c in comps if c.test_score is not None),
            reliability=_mean(c.reliability for c in comps if c.reliability is not None),
            efficiency=efficiency,
        )

    attempted = [r for r, _ in finished if r.attempt_no is not None]
    durations = sorted(r.duration_ms for r in attempted if r.duration_ms is not None)
    costs = [r.cost_usd for r in attempted if r.cost_usd is not None]

    per_task_rows: dict[uuid.UUID, list[tuple[ResultRecord, EvaluationStatus, bool]]] = defaultdict(
        list
    )
    for r, (st, cat) in finished:
        per_task_rows[r.task_id].append((r, st, cat is FailureCategory.PASSED))
    per_task = []
    for task_id in sorted({r.task_id for r in records}, key=str):
        rows = per_task_rows.get(task_id, [])
        graded_n = sum(st is EvaluationStatus.SUCCEEDED for _, st, _ in rows)
        passed_n = sum(p for _, st, p in rows if st is EvaluationStatus.SUCCEEDED)
        meta = tasks.get(task_id)
        per_task.append(
            TaskSummary(
                task_id=task_id,
                task_name=meta.name if meta else "",
                difficulty=meta.difficulty if meta else None,
                attempts=len(rows),
                graded=graded_n,
                passed=passed_n,
                success_rate=passed_n / graded_n if graded_n else None,
                mean_overall=_mean(
                    r.score_overall
                    for r, st, _ in rows
                    if st is EvaluationStatus.SUCCEEDED and r.score_overall is not None
                ),
            )
        )

    histogram = Counter(cat.value for _, (_, cat) in finished if cat is not None)
    return Summary(
        evaluation_id=evaluation_id,
        complete=in_progress == 0,
        attempts=stats.attempts,
        graded=stats.graded,
        passed=stats.passed,
        success_rate=stats.success_rate,
        ci95=stats.ci95,
        platform_failures=stats.platform_failures,
        platform_failure_rate=stats.platform_failure_rate,
        evaluation_errors=stats.evaluation_errors,
        ungraded=stats.ungraded,
        in_progress=in_progress,
        mean_score=mean_score,
        latency_ms=(
            Latency(p50=_nearest_rank(durations, 0.50), p95=_nearest_rank(durations, 0.95))
            if durations
            else None
        ),
        cost_usd=CostTotals(
            total=sum(costs, Decimal(0)),
            mean_per_attempt=(sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.000001"))
            if costs
            else None,
        ),
        tokens=TokenTotals(
            tokens_in=sum(r.tokens_in or 0 for r in attempted),
            tokens_out=sum(r.tokens_out or 0 for r in attempted),
        ),
        failure_categories=dict(sorted(histogram.items())),
        per_task=per_task,
    )


# -- the service --------------------------------------------------------------------------------


class EvaluationService:
    def __init__(self, session: AsyncSession, queue: RunQueue) -> None:
        self._s = session
        self._batches = BatchRepository(session)
        self._queue = queue

    async def create(self, data: EvaluationCreate, idempotency_key: str | None) -> CreateResult:
        """Batch row + every (task, repetition) run in ONE transaction, then
        dispatch. A replay (same key, same request) returns the batch and
        re-dispatches runs still QUEUED; a different request is a 409."""
        dataset = await DatasetRepository(self._s).get(data.dataset_id)
        if dataset is None:
            raise NotFound(f"dataset {data.dataset_id} not found")
        if dataset.status != DatasetStatus.FROZEN:
            raise Conflict(f"dataset {data.dataset_id} is not frozen")
        if await AgentVersionRepository(self._s).get(data.agent_version_id) is None:
            raise NotFound(f"agent version {data.agent_version_id} not found")
        task_ids = await DatasetRepository(self._s).task_ids(data.dataset_id)
        if len(task_ids) * data.repetitions > MAX_BATCH_RUNS:
            raise InvalidEvaluation(f"an evaluation can have at most {MAX_BATCH_RUNS} runs")
        config = data.scoring_config or DEFAULT_SCORING_CONFIG
        values = {
            "name": data.name,
            "dataset_id": data.dataset_id,
            "agent_version_id": data.agent_version_id,
            "repetitions": data.repetitions,
            "scoring_config": config.model_dump(mode="json"),
            "scoring_config_hash": config.config_hash(),
            "idempotency_key": idempotency_key,
        }

        batch = await self._batches.create(**values)
        created = batch is not None
        if batch is None:
            assert idempotency_key is not None  # a conflict needs a key
            batch = await self._batches.get_by_key(idempotency_key)
            assert batch is not None
            same = (
                batch.name,
                batch.dataset_id,
                batch.agent_version_id,
                batch.repetitions,
                batch.scoring_config_hash,
            ) == (
                data.name,
                data.dataset_id,
                data.agent_version_id,
                data.repetitions,
                values["scoring_config_hash"],
            )
            if not same:
                raise IdempotencyConflict(
                    f"Idempotency-Key {idempotency_key!r} was already used for a different "
                    f"evaluation ({batch.id})"
                )
        else:
            await self._batches.insert_runs(batch, task_ids, data.repetitions)
        await self._s.commit()

        queued = await self._batches.queued_run_ids(batch.id)
        try:
            for run_id in queued:
                await self._queue.publish(run_id)
        except RedisError as exc:
            log.error("evaluation_dispatch_failed", evaluation_id=str(batch.id), error=str(exc))
            raise DispatchUnavailable(batch.id, what="evaluation") from exc
        log.info(
            "evaluation_dispatched",
            evaluation_id=str(batch.id),
            runs=len(queued),
            replay=not created,
        )
        return CreateResult(await self._read(batch), created)

    async def get(self, evaluation_id: uuid.UUID) -> EvaluationRead:
        return await self._read(await self._get(evaluation_id))

    async def results(self, evaluation_id: uuid.UUID, after: str | None, limit: int) -> ResultPage:
        batch = await self._get(evaluation_id)
        cursor = _parse_cursor(after) if after else None
        rows = await self._batches.result_records(batch, after=cursor, limit=limit + 1)
        page = rows[:limit]
        return ResultPage(
            results=[_row(r, batch.scoring_config_hash) for r in page],
            next_after=f"{page[-1].task_id}:{page[-1].repetition}" if len(rows) > limit else None,
        )

    async def summary(self, evaluation_id: uuid.UUID) -> Summary:
        batch = await self._get(evaluation_id)
        return await self._summary(batch)

    async def compare(self, ids: list[uuid.UUID]) -> Compare:
        if not 2 <= len(ids) <= MAX_COMPARE or len(set(ids)) != len(ids):
            raise InvalidEvaluation(f"compare 2 to {MAX_COMPARE} distinct evaluations")
        batches = [await self._get(i) for i in ids]
        first = batches[0]
        if any(
            (b.dataset_id, b.scoring_config_hash) != (first.dataset_id, first.scoring_config_hash)
            for b in batches
        ):
            raise InvalidEvaluation(
                "evaluations can only be compared on the same dataset and scoring config"
            )
        summaries = [await self._summary(b) for b in batches]
        task_ids = await DatasetRepository(self._s).task_ids(first.dataset_id)
        rates = [{t.task_id: t.success_rate for t in s.per_task} for s in summaries]
        per_task = []
        for task_id in task_ids:
            by_eval = {b.id: rate.get(task_id) for b, rate in zip(batches, rates, strict=True)}
            base = by_eval[first.id]
            per_task.append(
                TaskComparison(
                    task_id=task_id,
                    success_rate=by_eval,
                    delta_vs_first={i: _delta(rate, base) for i, rate in by_eval.items()},
                )
            )
        return Compare(
            dataset_id=first.dataset_id,
            scoring_config_hash=first.scoring_config_hash,
            evaluations=[
                CompareEntry(evaluation_id=b.id, agent_version_id=b.agent_version_id, summary=s)
                for b, s in zip(batches, summaries, strict=True)
            ],
            per_task=per_task,
        )

    async def attempt_evaluation(self, run_id: uuid.UUID, attempt_no: int) -> AttemptEvaluationRead:
        run = await RunRepository(self._s).get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        config_hash = await self._config_hash_for(run)
        found = await self._batches.attempt_record(run_id, attempt_no, config_hash)
        if found is None:
            raise NotFound(f"no evaluation recorded for run {run_id} attempt {attempt_no}")
        record, created_at, finished_at = found
        public = _public(record, config_hash)
        assert public is not None
        return AttemptEvaluationRead(
            **public.model_dump(),
            attempt_no=attempt_no,
            created_at=created_at,
            finished_at=finished_at,
        )

    # -- internals ------------------------------------------------------------------------------

    async def _get(self, evaluation_id: uuid.UUID) -> Evaluation:
        batch = await self._batches.get(evaluation_id)
        if batch is None:
            raise NotFound(f"evaluation {evaluation_id} not found")
        return batch

    async def _config_hash_for(self, run: Run) -> str:
        if run.evaluation_id is None:
            return DEFAULT_SCORING_CONFIG.config_hash()
        return (await self._get(run.evaluation_id)).scoring_config_hash

    async def _summary(self, batch: Evaluation) -> Summary:
        records = await self._batches.result_records(batch)
        tasks = await self._batches.task_meta(sorted({r.task_id for r in records}, key=str))
        return summarize(batch.id, records, tasks)

    async def _read(self, batch: Evaluation) -> EvaluationRead:
        runs, evals, awaiting = await self._batches.progress_counts(batch.id)
        total = sum(runs.values())
        terminal = sum(n for s, n in runs.items() if s in TERMINAL)
        return EvaluationRead(
            id=batch.id,
            name=batch.name,
            dataset_id=batch.dataset_id,
            agent_version_id=batch.agent_version_id,
            repetitions=batch.repetitions,
            scoring_config=ScoringConfig.model_validate(batch.scoring_config),
            scoring_config_hash=batch.scoring_config_hash,
            created_at=batch.created_at,
            progress=Progress(
                runs_total=total,
                runs_by_status={RunStatus(s): runs.get(s, 0) for s in RunStatus},
                evaluations_by_status={s.value: evals.get(s.value, 0) for s in EvaluationStatus},
                awaiting_evaluation=awaiting,
                complete=terminal == total and awaiting == 0,
            ),
        )


def _row(r: ResultRecord, scoring_config_hash: str) -> ResultRow:
    return ResultRow(
        run_id=r.run_id,
        task_id=r.task_id,
        repetition=r.repetition,
        run_status=RunStatus(r.run_status),
        run_outcome=Outcome(r.run_outcome) if r.run_outcome else None,
        attempt_no=r.attempt_no,
        duration_ms=r.duration_ms,
        steps=r.steps,
        tokens_in=r.tokens_in,
        tokens_out=r.tokens_out,
        cost_usd=r.cost_usd,
        evaluation=_public(r, scoring_config_hash),
    )


def _parse_cursor(after: str) -> tuple[uuid.UUID, int]:
    try:
        task, rep = after.rsplit(":", 1)
        return uuid.UUID(task), int(rep)
    except ValueError:
        raise InvalidEvaluation("invalid results cursor") from None
