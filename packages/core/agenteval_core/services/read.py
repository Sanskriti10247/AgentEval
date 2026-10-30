"""The read API behind the dashboard (ADR-010 C): paged lists and the
overview stats. No writes, no commits. D11: every result leaves through a
public schema in schemas/read.py or schemas/evaluation.py, built field by
field; the ORM rows never reach a response.
"""

import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core import pagination
from agenteval_core.config import Settings
from agenteval_core.evaluation.taxonomy import DatasetStatus
from agenteval_core.repositories.batches import BatchRepository
from agenteval_core.repositories.read import Cursor, ReadRepository, WorkerCursor
from agenteval_core.schemas.evaluation import EvaluationPage, FinishRates
from agenteval_core.schemas.read import (
    ActivityBucket,
    ActivityEvaluation,
    AgentPage,
    AgentSummary,
    DatasetPage,
    DatasetSummary,
    EvaluationCounts,
    LatencyStats,
    LatestVersion,
    ProviderUsage,
    QueueDepth,
    RunCounts,
    StatsActivity,
    StatsOverview,
    SuccessStats,
    TaskListItem,
    TaskPage,
    UsageStats,
    WorkerLiveness,
    WorkerPage,
    WorkerRead,
)
from agenteval_core.services.errors import InvalidCursor
from agenteval_core.services.evaluations import context_of, evaluation_read, finish_rates
from agenteval_core.services.providers import is_simulated
from agenteval_core.states import RunStatus


def _cursor(after: str | None) -> Cursor | None:
    if not after:
        return None
    try:
        return pagination.decode_cursor(after)
    except ValueError:
        raise InvalidCursor("invalid cursor") from None


def _next(rows: list[tuple[datetime, uuid.UUID]], more: bool) -> str | None:
    return pagination.encode_cursor(*rows[-1]) if more and rows else None


# Evaluations listed by GET /stats/activity.
ACTIVITY_EVALUATIONS = 50


def activity_bucket_s(window_h: int) -> int:
    """Hourly buckets up to 48 h, six-hourly up to 7 days, daily beyond."""
    if window_h <= 48:
        return 3600
    if window_h <= 7 * 24:
        return 21600
    return 86400


def activity_starts(now: datetime, window_h: int, bucket_s: int) -> list[datetime]:
    """Bucket start times, oldest first: every multiple of bucket_s (Unix time) from the
    one at or before now - window_h up to the one containing now."""
    first = math.floor((now.timestamp() - window_h * 3600) / bucket_s)
    last = math.floor(now.timestamp() / bucket_s)
    return [datetime.fromtimestamp(i * bucket_s, UTC) for i in range(first, last + 1)]


def _worker_cursor(after: str | None) -> WorkerCursor | None:
    if not after:
        return None
    try:
        flag, stamp, worker_id = after.split("|", 2)
        if flag not in ("0", "1") or not worker_id:
            raise ValueError("bad worker cursor")
        return int(flag), pagination.parse_stamp(stamp), worker_id
    except ValueError:
        raise InvalidCursor("invalid cursor") from None


class ReadService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._settings = settings
        self._read = ReadRepository(session)
        self._batches = BatchRepository(session)

    async def agents(self, after: str | None, limit: int) -> AgentPage:
        rows = await self._read.agents_page(_cursor(after), limit + 1)
        page, more = pagination.split_page(rows, limit)
        latest = await self._read.latest_versions([a.id for a in page])
        items = []
        for a in page:
            v = latest.get(a.id)
            items.append(
                AgentSummary(
                    id=a.id,
                    name=a.name,
                    created_at=a.created_at,
                    latest_version=None
                    if v is None
                    else LatestVersion(
                        id=v.id,
                        version=v.version,
                        model_provider=v.model_provider,
                        model=v.model,
                        tools=list(v.tools),
                    ),
                )
            )
        return AgentPage(agents=items, next_after=_next([(a.created_at, a.id) for a in page], more))

    async def tasks(
        self,
        *,
        dataset_id: uuid.UUID | None,
        task_type: str | None,
        difficulty: str | None,
        after: str | None,
        limit: int,
    ) -> TaskPage:
        rows = await self._read.tasks_page(
            dataset_id=dataset_id,
            task_type=task_type,
            difficulty=difficulty,
            after=_cursor(after),
            limit=limit + 1,
        )
        page, more = pagination.split_page(rows, limit)
        items = [
            TaskListItem(
                id=t.id,
                name=t.name,
                description=t.description,
                task_type=t.task_type,
                difficulty=t.difficulty,
                dataset_id=t.dataset_id,
                image=t.image,
                timeout_s=t.timeout_s,
                memory_mb=t.memory_mb,
                cpu_limit=t.cpu_limit,
                created_at=t.created_at,
                has_evaluation=t.evaluation is not None,
            )
            for t in page
        ]
        return TaskPage(tasks=items, next_after=_next([(t.created_at, t.id) for t in page], more))

    async def datasets(
        self, *, status: DatasetStatus | None, after: str | None, limit: int
    ) -> DatasetPage:
        rows = await self._read.datasets_page(
            status=status.value if status else None, after=_cursor(after), limit=limit + 1
        )
        page, more = pagination.split_page(rows, limit)
        counts = await self._read.dataset_task_counts([d.id for d in page])
        items = [
            DatasetSummary(
                id=d.id,
                name=d.name,
                version=d.version,
                description=d.description,
                status=DatasetStatus(d.status),
                frozen_at=d.frozen_at,
                created_at=d.created_at,
                task_count=counts.get(d.id, 0),
            )
            for d in page
        ]
        return DatasetPage(
            datasets=items, next_after=_next([(d.created_at, d.id) for d in page], more)
        )

    async def evaluations(
        self,
        *,
        agent_version_id: uuid.UUID | None,
        dataset_id: uuid.UUID | None,
        after: str | None,
        limit: int,
    ) -> EvaluationPage:
        rows = await self._read.evaluations_page(
            agent_version_id=agent_version_id,
            dataset_id=dataset_id,
            after=_cursor(after),
            limit=limit + 1,
        )
        page, more = pagination.split_page(rows, limit)
        progress = await self._batches.progress_counts_many([b.id for b in page])
        contexts = await self._batches.contexts([b.id for b in page])
        return EvaluationPage(
            evaluations=[
                evaluation_read(b, *progress[b.id], context_of(contexts.get(b.id))) for b in page
            ],
            next_after=_next([(b.created_at, b.id) for b in page], more),
        )

    async def workers(self, after: str | None, limit: int) -> WorkerPage:
        rows = await self._read.workers_page(
            ttl_s=self._settings.lease_ttl_s, after=_worker_cursor(after), limit=limit + 1
        )
        page, more = pagination.split_page(rows, limit)
        items = [
            WorkerRead(
                id=w.id,
                hostname=w.hostname,
                version=w.version,
                concurrency=w.concurrency,
                started_at=w.started_at,
                last_heartbeat_at=w.last_heartbeat_at,
                stopped_at=w.stopped_at,
                stop_reason=w.stop_reason,
                alive=alive,
            )
            for w, alive in page
        ]
        next_after = None
        if more:
            last = page[-1][0]
            flag = 0 if last.stopped_at is None else 1
            next_after = f"{flag}|{pagination.format_stamp(last.started_at)}|{last.id}"
        return WorkerPage(workers=items, next_after=next_after)

    async def overview(self, window_h: int) -> StatsOverview:
        """Definitions are in docs/api/read.md. One aggregate statement per group."""
        now = datetime.now(UTC)
        since = now - timedelta(hours=window_h)
        ttl = self._settings.lease_ttl_s
        by_status = await self._read.runs_by_status()
        batches, cancel_requested = await self._read.evaluation_counts()
        pending = await self._read.pending_evaluations()
        active = await self._read.active_workers(ttl)
        alive, stale = await self._read.worker_liveness(ttl)
        oldest_queued = await self._read.oldest_queued_age_s()
        count, p50, p95, p99, mean = await self._read.run_latency(since)
        attempts, cost, tokens_in, tokens_out = await self._read.usage(since)
        by_provider = await self._read.usage_by_provider(since)
        graded, passed = await self._read.graded(since)
        finished, completed, failed, timed_out, retried = await self._read.finish_counts(since)
        queued = by_status.get(RunStatus.QUEUED, 0)
        retrying = by_status.get(RunStatus.RETRYING, 0)
        return StatsOverview(
            window_h=window_h,
            generated_at=now,
            evaluations=EvaluationCounts(total=batches, cancel_requested=cancel_requested),
            runs=RunCounts(
                total=sum(by_status.values()),
                by_status={s: by_status.get(s, 0) for s in RunStatus},
            ),
            success=SuccessStats(
                graded=graded,
                passed=passed,
                success_rate=passed / graded if graded else None,
            ),
            run_latency_ms=LatencyStats(count=count, p50=p50, p95=p95, p99=p99, mean=mean),
            usage=UsageStats(
                attempts=attempts,
                cost_usd=cost,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                by_provider={
                    provider: ProviderUsage(
                        attempts=n,
                        tokens_in=t_in,
                        tokens_out=t_out,
                        cost_usd=c,
                        simulated=is_simulated(provider),
                    )
                    for provider, (n, c, t_in, t_out) in by_provider.items()
                },
            ),
            active_workers=active,
            queue=QueueDepth(
                runs_queued=queued,
                runs_retrying=retrying,
                evaluations_pending=pending,
                total=queued + retrying + pending,
                oldest_queued_s=oldest_queued,
            ),
            rates=FinishRates(
                **finish_rates(
                    finished=finished,
                    completed=completed,
                    failed=failed,
                    timed_out=timed_out,
                    retried=retried,
                )
            ),
            workers=WorkerLiveness(alive=alive, stale=stale),
        )

    async def activity(self, window_h: int) -> StatsActivity:
        """Runs per time bucket and the evaluations created in the window
        (docs/api/read.md). Buckets are bucketed by run finished_at."""
        now = datetime.now(UTC)
        bucket_s = activity_bucket_s(window_h)
        starts = activity_starts(now, window_h, bucket_s)
        rows = {int(r.bucket): r for r in await self._read.activity_buckets(starts[0], bucket_s)}
        buckets = []
        for start in starts:
            r = rows.get(int(start.timestamp()) // bucket_s)
            buckets.append(
                ActivityBucket(
                    start=start,
                    finished=r.finished if r else 0,
                    completed=r.completed if r else 0,
                    failed=r.failed if r else 0,
                    cancelled=r.cancelled if r else 0,
                    graded=r.graded if r else 0,
                    passed=r.passed if r else 0,
                    p95_ms=None if r is None or r.p95_ms is None else float(r.p95_ms),
                )
            )
        recent = await self._read.recent_evaluations(
            now - timedelta(hours=window_h), ACTIVITY_EVALUATIONS
        )
        outcomes = await self._read.evaluation_outcomes([e.id for e in recent])
        evaluations = []
        for e in recent:
            runs, graded, passed, p95 = outcomes.get(e.id, (0, 0, 0, None))
            evaluations.append(
                ActivityEvaluation(
                    id=e.id,
                    name=e.name,
                    created_at=e.created_at,
                    agent_name=e.agent_name,
                    agent_version=e.agent_version,
                    provider=e.provider,
                    model=e.model,
                    simulated=is_simulated(e.provider),
                    runs_total=runs,
                    graded=graded,
                    passed=passed,
                    success_rate=passed / graded if graded else None,
                    p95_ms=p95,
                )
            )
        return StatsActivity(
            window_h=window_h,
            bucket_s=bucket_s,
            generated_at=now,
            buckets=buckets,
            evaluations=evaluations,
        )
