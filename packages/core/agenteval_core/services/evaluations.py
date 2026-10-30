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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.agent.limits import AgentLimits
from agenteval_core.agent.pricing import PRICING_VERSION, is_priced
from agenteval_core.config import Settings
from agenteval_core.evaluation import EVALUATOR_VERSION
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
from agenteval_core.models import AgentVersion, Evaluation, Run
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.repositories.agents import AgentVersionRepository
from agenteval_core.repositories.batches import (
    BatchRepository,
    ContextRecord,
    ResultRecord,
    TaskMeta,
)
from agenteval_core.repositories.datasets import DatasetRepository
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.schemas.evaluation import (
    MAX_BATCH_RUNS,
    MAX_COMPARE,
    AttemptEvaluationRead,
    Compare,
    CompareDifference,
    CompareEntry,
    CostTotals,
    EvaluationContext,
    EvaluationCreate,
    EvaluationEstimate,
    EvaluationEstimateRequest,
    EvaluationRead,
    Latency,
    MeanScore,
    Progress,
    ResultPage,
    ResultRow,
    RunRates,
    Summary,
    TaskComparison,
    TaskSummary,
    TokenTotals,
)
from agenteval_core.services.dispatch import stamp_dispatched
from agenteval_core.services.errors import (
    Conflict,
    DispatchUnavailable,
    IdempotencyConflict,
    InvalidEvaluation,
    NotFound,
)
from agenteval_core.services.providers import availability, is_simulated
from agenteval_core.states import Outcome, RunStatus

log = get_logger(__name__)

# CANCELLED counts as terminal for `complete` (Step 8): a cancelled batch completes.
TERMINAL = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})
_UNFINISHED_EVAL = frozenset({EvaluationStatus.PENDING, EvaluationStatus.RUNNING})
# Statuses whose run has a result of its own (F13). In every other status the
# run's latest attempt (attempt_no = attempt_count) is either an earlier, failed
# attempt that is being retried or replayed (QUEUED, CLAIMED, RETRYING) or one
# that was cancelled with the run (CANCELLED): that attempt is history, not the
# run's result, so it is not counted, summarised or shown as the result.
# RUNNING is the exception that has no result yet either, but its latest attempt
# is the live one and has no evaluation, so it counts as in progress anyway.
_HAS_RESULT = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})
_NO_RESULT = frozenset(
    {RunStatus.QUEUED, RunStatus.CLAIMED, RunStatus.RETRYING, RunStatus.CANCELLED}
)


# Runs cancelled per transaction by a batch cancel: bounded so no single
# statement holds many row locks or runs long (ADR-008 spirit).
CANCEL_CHUNK = 100


@dataclass(frozen=True)
class CancelBatchResult:
    evaluation: EvaluationRead
    accepted: bool  # True: this call changed something (202); False: idempotent repeat (200)


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
    any attempt has no evaluation row: Step 3's platform_category() decides.
    A run that is not (yet) COMPLETED or FAILED has no result either (F13): a
    RETRYING run is not finished, whatever its earlier attempt's evaluation says."""
    if r.run_status not in _HAS_RESULT:
        return None
    if r.eval_status is not None:
        status = EvaluationStatus(r.eval_status)
        if status in _UNFINISHED_EVAL:
            return None
        category = FailureCategory(r.failure_category) if r.failure_category else None
        return status, category
    if r.run_status == RunStatus.FAILED and r.attempt_no is None and r.run_outcome:
        reason = FailureReason(r.attempt_failure_reason) if r.attempt_failure_reason else None
        facts = AttemptFacts(Outcome(r.run_outcome), r.run_error_message, failure_reason=reason)
        category = platform_category(facts)
        if category is not None:
            return EvaluationStatus.SKIPPED, category
    return None


def nearest_rank(sorted_values: Sequence[int], p: float) -> int:
    """The p-th percentile by nearest rank: the value at rank ceil(p x n), 1-based, of
    the ascending values (SQL's percentile_disc). Needs at least one value."""
    return sorted_values[max(0, math.ceil(p * len(sorted_values)) - 1)]


def _ratio(n: int, d: int) -> float | None:
    return n / d if d else None


def finish_rates(
    *, finished: int, completed: int, failed: int, timed_out: int, retried: int
) -> dict[str, Any]:
    """FinishRates fields from counts over finished (COMPLETED or FAILED) runs; every
    rate is None when nothing finished. Shared with the overview."""
    return {
        "finished": finished,
        "completed": completed,
        "failed": failed,
        "timed_out": timed_out,
        "retried": retried,
        "completion_rate": _ratio(completed, finished),
        "failure_rate": _ratio(failed, finished),
        "timeout_rate": _ratio(timed_out, finished),
        "retry_rate": _ratio(retried, finished),
    }


def run_rates(records: Sequence[ResultRecord]) -> RunRates:
    """Run-level rates of a batch. A run's attempt_no here is its latest attempt's, so
    `retried` counts runs that finished on attempt 2 or later (a retry or a replay)."""
    done = [r for r in records if r.run_status in _HAS_RESULT]
    return RunRates(
        scheduled=sum(r.run_status != RunStatus.CANCELLED for r in records),
        **finish_rates(
            finished=len(done),
            completed=sum(r.run_status == RunStatus.COMPLETED for r in done),
            failed=sum(r.run_status == RunStatus.FAILED for r in done),
            timed_out=sum(r.run_outcome == Outcome.TIMEOUT for r in done),
            retried=sum((r.attempt_no or 0) > 1 for r in done),
        ),
    )


def latency(durations: Sequence[int]) -> Latency | None:
    """Nearest-rank p50/p95/p99, mean and count of ascending `durations`."""
    if not durations:
        return None
    return Latency(
        p50=nearest_rank(durations, 0.50),
        p95=nearest_rank(durations, 0.95),
        p99=nearest_rank(durations, 0.99),
        mean=sum(durations) / len(durations),
        count=len(durations),
    )


def _mean(values: Iterable[Decimal]) -> Decimal | None:
    vals = list(values)
    return (sum(vals, Decimal(0)) / len(vals)).quantize(PLACES) if vals else None


def _delta(rate: float | None, base: float | None) -> float | None:
    return None if rate is None or base is None else rate - base


def summarize(
    evaluation_id: uuid.UUID,
    records: Sequence[ResultRecord],
    tasks: dict[uuid.UUID, TaskMeta],
    *,
    simulated: bool = False,
) -> Summary:
    finished = [(r, res) for r in records if (res := _result_of(r)) is not None]
    stats = success_stats(res for _, res in finished)
    # A CANCELLED run is terminal with no result (F13): neither finished nor in progress,
    # so a cancelled batch's summary completes.
    cancelled = sum(r.run_status == RunStatus.CANCELLED for r in records)
    in_progress = len(records) - len(finished) - cancelled

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
        latency_ms=latency(durations),
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
        rates=run_rates(records),
        simulated=simulated,
    )


def upper_bounds(
    runs: int, limits: AgentLimits, run_max_attempts: int, *, priced: bool
) -> tuple[int, Decimal | None]:
    """(model requests, cost in USD) an evaluation can use at most: every run takes
    every attempt it may get, and each attempt every step and its whole cost cap. The
    cost is None when the model has no price (cost could not be tracked)."""
    requests = runs * limits.max_steps * run_max_attempts
    cost = runs * limits.max_cost_usd * run_max_attempts if priced else None
    return requests, cost


# -- comparison (pure) ---------------------------------------------------------------------------

# Fewer graded attempts than this and a comparison carries a warning.
MIN_GRADED_FOR_COMPARE = 30
DIFFERENCE_FIELDS = ("provider", "model", "agent_config_hash", "agent_name", "repetitions")


def intervals_disjoint(a: tuple[float, float] | None, b: tuple[float, float] | None) -> bool | None:
    """True when two 95% intervals do not overlap; None when either is missing."""
    if a is None or b is None:
        return None
    return a[1] < b[0] or b[1] < a[0]


def compare_differences(
    facts: Sequence[tuple[uuid.UUID, Mapping[str, str | int | None]]],
) -> list[CompareDifference]:
    """The DIFFERENCE_FIELDS whose values are not all equal, with every entry's value."""
    out = []
    for name in DIFFERENCE_FIELDS:
        values = {eid: f.get(name) for eid, f in facts}
        if len(set(values.values())) > 1:
            out.append(CompareDifference(field=name, values=values))
    return out


def compare_warnings(entries: Sequence[tuple[str, Summary]]) -> list[str]:
    """Reasons to read a comparison with care, from (evaluation name, summary) pairs."""
    warnings = []
    if len({s.simulated for _, s in entries}) > 1:
        warnings.append(
            "Mixes simulated (fake-model) and real-model evaluations; their scores and "
            "costs are not comparable"
        )
    for name, s in entries:
        if s.graded < MIN_GRADED_FOR_COMPARE:
            warnings.append(
                f"{name} has only {s.graded} graded attempts; treat differences as indicative"
            )
    for name, s in entries:
        if not s.complete:
            warnings.append(f"{name} is not complete; its numbers can still change")
    return warnings


def context_of(record: ContextRecord | None) -> EvaluationContext | None:
    if record is None:
        return None
    return EvaluationContext(
        agent_id=record.agent_id,
        agent_name=record.agent_name,
        agent_version=record.agent_version,
        agent_config_hash=record.agent_config_hash,
        provider=record.provider,
        model=record.model,
        simulated=is_simulated(record.provider),
        dataset_name=record.dataset_name,
        dataset_version=record.dataset_version,
        task_count=record.task_count,
        evaluator_version=EVALUATOR_VERSION,
        pricing_version=PRICING_VERSION,
    )


# -- the service --------------------------------------------------------------------------------


class EvaluationService:
    def __init__(self, session: AsyncSession, queue: RunQueue, settings: Settings) -> None:
        self._s = session
        self._settings = settings
        self._batches = BatchRepository(session)
        self._queue = queue

    async def create(self, data: EvaluationCreate, idempotency_key: str | None) -> CreateResult:
        """Batch row + every (task, repetition) run in ONE transaction, then
        dispatch. A replay (same key, same request) returns the batch and
        re-dispatches runs still QUEUED; a different request is a 409."""
        version, task_ids = await self._launchable(
            data.dataset_id, data.agent_version_id, data.repetitions
        )
        provider = version.model_provider
        if provider not in self._settings.enabled_providers():
            raise InvalidEvaluation(f"Provider '{provider}' is not enabled on this server")
        config = data.scoring_config or DEFAULT_SCORING_CONFIG
        run_budget = data.run_max_attempts or self._settings.run_max_attempts
        eval_budget = data.eval_max_attempts or self._settings.eval_max_attempts
        # A real model always runs under a cap; a fake one only if the request names one.
        max_cost = data.max_cost_usd
        if max_cost is None and not is_simulated(provider):
            max_cost = self._settings.real_eval_max_cost_usd
        values = {
            "name": data.name,
            "dataset_id": data.dataset_id,
            "agent_version_id": data.agent_version_id,
            "repetitions": data.repetitions,
            "scoring_config": config.model_dump(mode="json"),
            "scoring_config_hash": config.config_hash(),
            "idempotency_key": idempotency_key,
            "run_max_attempts": run_budget,  # F6, G1
            "eval_max_attempts": eval_budget,  # F9, G1
            "max_cost_usd": max_cost,
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
                batch.run_max_attempts,
                batch.eval_max_attempts,
                batch.max_cost_usd,
            ) == (
                data.name,
                data.dataset_id,
                data.agent_version_id,
                data.repetitions,
                values["scoring_config_hash"],
                run_budget,
                eval_budget,
                max_cost,
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
        await stamp_dispatched(self._s, queued)
        log.info(
            "evaluation_dispatched",
            evaluation_id=str(batch.id),
            runs=len(queued),
            replay=not created,
        )
        return CreateResult(await self._read(batch), created)

    async def estimate(self, data: EvaluationEstimateRequest) -> EvaluationEstimate:
        """Upper bounds for an evaluation that POST /evaluations would accept; the same
        404 / 409 / 422 for a missing version or dataset, an unfrozen dataset or too
        many runs. An unavailable provider is not an error here: `available` says so."""
        version, task_ids = await self._launchable(
            data.dataset_id, data.agent_version_id, data.repetitions
        )
        provider, model = version.model_provider, version.model
        limits = AgentLimits.model_validate(version.limits)
        attempts = data.run_max_attempts or self._settings.run_max_attempts
        runs = len(task_ids) * data.repetitions
        priced = is_priced(provider, model)
        simulated = is_simulated(provider)
        max_requests, max_cost = upper_bounds(runs, limits, attempts, priced=priced)
        return EvaluationEstimate(
            tasks=len(task_ids),
            repetitions=data.repetitions,
            runs=runs,
            provider=provider,
            model=model,
            simulated=simulated,
            available=availability(self._settings, provider)[0],
            max_steps_per_run=limits.max_steps,
            run_max_attempts=attempts,
            max_model_requests=max_requests,
            pricing_known=priced,
            pricing_version=PRICING_VERSION,
            max_cost_usd_per_run=limits.max_cost_usd,
            max_cost_usd=max_cost,
            default_budget_usd=None if simulated else self._settings.real_eval_max_cost_usd,
            sequential=not simulated,
        )

    async def get(self, evaluation_id: uuid.UUID) -> EvaluationRead:
        return await self._read(await self._get(evaluation_id))

    async def cancel(self, evaluation_id: uuid.UUID) -> CancelBatchResult:
        """Cancel a batch (F15): flag the batch (cancel_reason 'user'), then
        cancel every non-terminal run with reason 'batch' in chunks of
        CANCEL_CHUNK, one transaction each. Terminal runs are never touched
        and already-written evaluations and scores stay as they are (F5:
        cancellation never mutates metrics). Repeatable: a second call
        finishes whatever a crashed first call left and changes nothing else."""
        await self._get(evaluation_id)  # 404 before any write
        changed = await self._batches.flag_cancel(evaluation_id)
        await self._s.commit()
        while True:
            selected, cancelled = await self._batches.cancel_runs_chunk(evaluation_id, CANCEL_CHUNK)
            await self._s.commit()
            changed = changed or cancelled > 0
            if selected == 0:
                break
        batch = await self._get(evaluation_id)
        if changed:
            log.info("evaluation_cancel_requested", evaluation_id=str(evaluation_id))
        return CancelBatchResult(await self._read(batch), accepted=changed)

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
        records = await self._batches.contexts([b.id for b in batches])
        contexts = [context_of(records.get(b.id)) for b in batches]
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
        first_ci = summaries[0].ci95
        names = [(b.name, s) for b, s in zip(batches, summaries, strict=True)]
        facts = [
            (
                b.id,
                {
                    "provider": c.provider if c else None,
                    "model": c.model if c else None,
                    "agent_config_hash": c.agent_config_hash if c else None,
                    "agent_name": c.agent_name if c else None,
                    "repetitions": b.repetitions,
                },
            )
            for b, c in zip(batches, contexts, strict=True)
        ]
        return Compare(
            dataset_id=first.dataset_id,
            scoring_config_hash=first.scoring_config_hash,
            evaluations=[
                CompareEntry(
                    evaluation_id=b.id,
                    agent_version_id=b.agent_version_id,
                    summary=s,
                    context=c,
                    distinguishable_from_first=(
                        None if i == 0 else intervals_disjoint(first_ci, s.ci95)
                    ),
                )
                for i, (b, s, c) in enumerate(zip(batches, summaries, contexts, strict=True))
            ],
            per_task=per_task,
            differences=compare_differences(facts),
            warnings=compare_warnings(names),
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

    async def _launchable(
        self, dataset_id: uuid.UUID, agent_version_id: uuid.UUID, repetitions: int
    ) -> tuple[AgentVersion, list[uuid.UUID]]:
        """The checks create and estimate share: a FROZEN dataset (404, 409), an existing
        version (404) and at most MAX_BATCH_RUNS runs (422)."""
        dataset = await DatasetRepository(self._s).get(dataset_id)
        if dataset is None:
            raise NotFound(f"dataset {dataset_id} not found")
        if dataset.status != DatasetStatus.FROZEN:
            raise Conflict(f"dataset {dataset_id} is not frozen")
        version = await AgentVersionRepository(self._s).get(agent_version_id)
        if version is None:
            raise NotFound(f"agent version {agent_version_id} not found")
        task_ids = await DatasetRepository(self._s).task_ids(dataset_id)
        if len(task_ids) * repetitions > MAX_BATCH_RUNS:
            raise InvalidEvaluation(f"an evaluation can have at most {MAX_BATCH_RUNS} runs")
        return version, task_ids

    async def _config_hash_for(self, run: Run) -> str:
        if run.evaluation_id is None:
            return DEFAULT_SCORING_CONFIG.config_hash()
        return (await self._get(run.evaluation_id)).scoring_config_hash

    async def _summary(self, batch: Evaluation) -> Summary:
        records = await self._batches.result_records(batch)
        tasks = await self._batches.task_meta(sorted({r.task_id for r in records}, key=str))
        version = await AgentVersionRepository(self._s).get(batch.agent_version_id)
        simulated = version is not None and is_simulated(version.model_provider)
        return summarize(batch.id, records, tasks, simulated=simulated)

    async def _read(self, batch: Evaluation) -> EvaluationRead:
        runs, evals, awaiting = await self._batches.progress_counts(batch.id)
        contexts = await self._batches.contexts([batch.id])
        return evaluation_read(batch, runs, evals, awaiting, context_of(contexts.get(batch.id)))


def evaluation_read(
    batch: Evaluation,
    runs: dict[str, int],
    evals: dict[str, int],
    awaiting: int,
    context: EvaluationContext | None = None,
) -> EvaluationRead:
    """The one place a batch row becomes API output (get, create, cancel, list)."""
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
        cancel_requested_at=batch.cancel_requested_at,
        run_max_attempts=batch.run_max_attempts,
        eval_max_attempts=batch.eval_max_attempts,
        progress=Progress(
            runs_total=total,
            runs_by_status={RunStatus(s): runs.get(s, 0) for s in RunStatus},
            evaluations_by_status={s.value: evals.get(s.value, 0) for s in EvaluationStatus},
            awaiting_evaluation=awaiting,
            complete=terminal == total and awaiting == 0,
        ),
        max_cost_usd=batch.max_cost_usd,
        context=context,
    )


def _row(r: ResultRecord, scoring_config_hash: str) -> ResultRow:
    """One results row. A run that is QUEUED/CLAIMED/RETRYING/CANCELLED has no
    result (F13): its latest attempt is history, so the attempt and evaluation
    columns are blank rather than a stale failed attempt shown as the result."""
    if r.run_status in _NO_RESULT:
        return ResultRow(
            run_id=r.run_id,
            task_id=r.task_id,
            repetition=r.repetition,
            run_status=RunStatus(r.run_status),
            run_outcome=None,
            attempt_no=None,
            duration_ms=None,
            steps=None,
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
            evaluation=None,
        )
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
