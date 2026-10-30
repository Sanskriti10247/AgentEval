"""Read-only list and aggregate queries behind the dashboard API (ADR-010 C).

Every list is keyset-paged newest first on (created_at DESC, id DESC) and
fetches `limit` rows as given; the service asks for one extra to learn whether
another page exists. Nothing here writes, and nothing selects an internal
column that a public schema would then have to hide.

At project scale (thousands of rows) the primary key plus a sort covers these
lists, so no migration adds an index for them.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Select, and_, case, cast, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import (
    Agent,
    AgentVersion,
    AttemptEvaluation,
    Dataset,
    Evaluation,
    Run,
    RunAttempt,
    Task,
    Worker,
)
from agenteval_core.states import Outcome, RunStatus

Cursor = tuple[datetime, uuid.UUID]
WorkerCursor = tuple[int, datetime, str]


def _newest_first(stmt: Select[Any], model: Any, after: Cursor | None, limit: int) -> Select[Any]:
    stmt = stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit)
    if after is not None:
        stmt = stmt.where(tuple_(model.created_at, model.id) < tuple_(*after))
    return stmt


def _stopped_flag() -> Any:
    """0 for a worker that has not stopped, 1 for one that has: live sorts first."""
    return case((Worker.stopped_at.is_(None), 0), else_=1)


def _alive(ttl_s: float) -> Any:
    """Not stopped, and a heartbeat within 3 x the lease TTL (database clock)."""
    return and_(
        Worker.stopped_at.is_(None),
        Worker.last_heartbeat_at > func.now() - timedelta(seconds=3 * ttl_s),
    )


_FINISHED = (RunStatus.COMPLETED, RunStatus.FAILED)
_RUN_MS = func.extract("epoch", Run.finished_at - Run.started_at) * 1000


def _latest_attempt() -> Any:
    """Join condition: a run's latest attempt (attempt_no = attempt_count)."""
    return and_(RunAttempt.run_id == Run.id, RunAttempt.attempt_no == Run.attempt_count)


def _float(v: Any) -> float | None:
    return None if v is None else float(v)


class ReadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def agents_page(self, after: Cursor | None, limit: int) -> list[Agent]:
        stmt = _newest_first(select(Agent), Agent, after, limit)
        return list((await self._s.execute(stmt)).scalars())

    async def latest_versions(self, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, AgentVersion]:
        """Highest version number per agent, one query for the whole page."""
        if not agent_ids:
            return {}
        stmt = (
            select(AgentVersion)
            .ext(distinct_on(AgentVersion.agent_id))
            .where(AgentVersion.agent_id.in_(agent_ids))
            .order_by(AgentVersion.agent_id, AgentVersion.version.desc())
        )
        return {v.agent_id: v for v in (await self._s.execute(stmt)).scalars()}

    async def tasks_page(
        self,
        *,
        dataset_id: uuid.UUID | None,
        task_type: str | None,
        difficulty: str | None,
        after: Cursor | None,
        limit: int,
    ) -> list[Task]:
        stmt = select(Task)
        if dataset_id is not None:
            stmt = stmt.where(Task.dataset_id == dataset_id)
        if task_type is not None:
            stmt = stmt.where(Task.task_type == task_type)
        if difficulty is not None:
            stmt = stmt.where(Task.difficulty == difficulty)
        stmt = _newest_first(stmt, Task, after, limit)
        return list((await self._s.execute(stmt)).scalars())

    async def datasets_page(
        self, *, status: str | None, after: Cursor | None, limit: int
    ) -> list[Dataset]:
        stmt = select(Dataset)
        if status is not None:
            stmt = stmt.where(Dataset.status == status)
        stmt = _newest_first(stmt, Dataset, after, limit)
        return list((await self._s.execute(stmt)).scalars())

    async def dataset_task_counts(self, dataset_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not dataset_ids:
            return {}
        rows = await self._s.execute(
            select(Task.dataset_id, func.count())
            .where(Task.dataset_id.in_(dataset_ids))
            .group_by(Task.dataset_id)
        )
        return {d: int(n) for d, n in rows.all() if d is not None}

    async def evaluations_page(
        self,
        *,
        agent_version_id: uuid.UUID | None,
        dataset_id: uuid.UUID | None,
        after: Cursor | None,
        limit: int,
    ) -> list[Evaluation]:
        stmt = select(Evaluation)
        if agent_version_id is not None:
            stmt = stmt.where(Evaluation.agent_version_id == agent_version_id)
        if dataset_id is not None:
            stmt = stmt.where(Evaluation.dataset_id == dataset_id)
        stmt = _newest_first(stmt, Evaluation, after, limit)
        return list((await self._s.execute(stmt)).scalars())

    async def workers_page(
        self, *, ttl_s: float, after: WorkerCursor | None, limit: int
    ) -> list[tuple[Worker, bool]]:
        """Live workers first, then newest started first; keyset on
        (stopped flag asc, started_at desc, id desc)."""
        flag = _stopped_flag()
        stmt = (
            select(Worker, _alive(ttl_s).label("alive"))
            .order_by(flag, Worker.started_at.desc(), Worker.id.desc())
            .limit(limit)
        )
        if after is not None:
            after_flag, started, worker_id = after
            stmt = stmt.where(
                or_(
                    flag > after_flag,
                    and_(
                        flag == after_flag,
                        tuple_(Worker.started_at, Worker.id) < tuple_(started, worker_id),
                    ),
                )
            )
        return [(w, bool(a)) for w, a in (await self._s.execute(stmt)).all()]

    # Stats: one statement per group.

    async def runs_by_status(self) -> dict[str, int]:
        rows = await self._s.execute(select(Run.status, func.count()).group_by(Run.status))
        return {str(s): int(n) for s, n in rows.all()}

    async def evaluation_counts(self) -> tuple[int, int]:
        """(batches, batches with a cancel request)."""
        row = (
            await self._s.execute(
                select(func.count(), func.count(Evaluation.cancel_requested_at)).select_from(
                    Evaluation
                )
            )
        ).one()
        return int(row[0]), int(row[1])

    async def pending_evaluations(self) -> int:
        result = await self._s.execute(
            select(func.count())
            .select_from(AttemptEvaluation)
            .where(AttemptEvaluation.status == "PENDING")
        )
        return int(result.scalar_one())

    async def active_workers(self, ttl_s: float) -> int:
        result = await self._s.execute(
            select(func.count()).select_from(Worker).where(_alive(ttl_s))
        )
        return int(result.scalar_one())

    async def worker_liveness(self, ttl_s: float) -> tuple[int, int]:
        """(alive, stale): workers not stopped, with a heartbeat inside the liveness
        window (3 x the lease TTL) and outside it."""
        row = (
            await self._s.execute(
                select(
                    func.count().filter(_alive(ttl_s)),
                    func.count().filter(Worker.stopped_at.is_(None), ~_alive(ttl_s)),
                ).select_from(Worker)
            )
        ).one()
        return int(row[0]), int(row[1])

    async def oldest_queued_age_s(self) -> float | None:
        """Seconds since the oldest QUEUED run was created (database clock)."""
        age = await self._s.scalar(
            select(func.extract("epoch", func.now() - func.min(Run.created_at))).where(
                Run.status == RunStatus.QUEUED
            )
        )
        return _float(age)

    async def run_latency(
        self, since: datetime
    ) -> tuple[int, float | None, float | None, float | None, float | None]:
        """(count, p50, p95, p99, mean) of finished_at - started_at in ms, over COMPLETED
        and FAILED runs that started and finished since `since`. Percentiles are
        nearest-rank (percentile_disc): always a value that was observed."""
        row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.percentile_disc(0.5).within_group(_RUN_MS),
                    func.percentile_disc(0.95).within_group(_RUN_MS),
                    func.percentile_disc(0.99).within_group(_RUN_MS),
                    func.avg(_RUN_MS),
                ).where(
                    Run.status.in_(_FINISHED),
                    Run.started_at.is_not(None),
                    Run.finished_at >= since,
                )
            )
        ).one()
        return int(row[0]), _float(row[1]), _float(row[2]), _float(row[3]), _float(row[4])

    async def finish_counts(self, since: datetime) -> tuple[int, int, int, int, int]:
        """(finished, completed, failed, timed out, retried) over COMPLETED and FAILED
        runs that finished since `since`; retried = finished on attempt 2 or later."""
        row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.count().filter(Run.status == RunStatus.COMPLETED),
                    func.count().filter(Run.status == RunStatus.FAILED),
                    func.count().filter(Run.outcome == Outcome.TIMEOUT),
                    func.count().filter(Run.attempt_count > 1),
                ).where(Run.status.in_(_FINISHED), Run.finished_at >= since)
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4])

    async def usage_by_provider(self, since: datetime) -> dict[str, tuple[int, Decimal, int, int]]:
        """usage() per model provider: provider -> (attempts, cost, tokens in, tokens
        out). Agent attempts only: a command task's run has no agent version."""
        rows = await self._s.execute(
            select(
                AgentVersion.model_provider,
                func.count(),
                func.coalesce(func.sum(RunAttempt.cost_usd), 0),
                cast(func.coalesce(func.sum(RunAttempt.tokens_in), 0), BigInteger),
                cast(func.coalesce(func.sum(RunAttempt.tokens_out), 0), BigInteger),
            )
            .select_from(RunAttempt)
            .join(Run, Run.id == RunAttempt.run_id)
            .join(AgentVersion, AgentVersion.id == Run.agent_version_id)
            .where(RunAttempt.finished_at >= since)
            .group_by(AgentVersion.model_provider)
            .order_by(AgentVersion.model_provider)
        )
        return {
            str(p): (int(n), Decimal(str(cost)), int(t_in), int(t_out))
            for p, n, cost, t_in, t_out in rows.all()
        }

    async def activity_buckets(self, start: datetime, bucket_s: int) -> list[Any]:
        """Per bucket of `bucket_s` seconds (index = floor(epoch(finished_at) / bucket_s))
        of runs that finished at or after `start`: finished, completed, failed, cancelled,
        graded, passed and the nearest-rank p95 of run latency. Only non-empty buckets."""
        bucket = func.floor(func.extract("epoch", Run.finished_at) / bucket_s).label("bucket")
        finished = Run.status.in_(_FINISHED)
        graded = and_(finished, AttemptEvaluation.status == "SUCCEEDED")
        stmt = (
            select(
                bucket,
                func.count().filter(finished).label("finished"),
                func.count().filter(Run.status == RunStatus.COMPLETED).label("completed"),
                func.count().filter(Run.status == RunStatus.FAILED).label("failed"),
                func.count().filter(Run.status == RunStatus.CANCELLED).label("cancelled"),
                func.count().filter(graded).label("graded"),
                func.count()
                .filter(graded, AttemptEvaluation.task_success.is_(True))
                .label("passed"),
                func.percentile_disc(0.95)
                .within_group(_RUN_MS)
                .filter(finished, Run.started_at.is_not(None))
                .label("p95_ms"),
            )
            .select_from(Run)
            .outerjoin(RunAttempt, _latest_attempt())
            .outerjoin(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
            .where(Run.finished_at >= start)
            .group_by(bucket)
        )
        return list((await self._s.execute(stmt)).all())

    async def recent_evaluations(self, since: datetime, limit: int) -> list[Any]:
        """Batches created since `since`, newest first, with their agent version."""
        stmt = (
            select(
                Evaluation.id,
                Evaluation.name,
                Evaluation.created_at,
                Agent.name.label("agent_name"),
                AgentVersion.version.label("agent_version"),
                AgentVersion.model_provider.label("provider"),
                AgentVersion.model,
            )
            .join(AgentVersion, AgentVersion.id == Evaluation.agent_version_id)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(Evaluation.created_at >= since)
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
            .limit(limit)
        )
        return list((await self._s.execute(stmt)).all())

    async def evaluation_outcomes(
        self, evaluation_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int, int, float | None]]:
        """Per batch: (runs, graded, passed, nearest-rank p95 of attempt duration_ms).
        graded / passed follow graded(): the latest attempt of a COMPLETED or FAILED run
        whose evaluation SUCCEEDED; the p95 is over those runs' latest attempts."""
        if not evaluation_ids:
            return {}
        finished = Run.status.in_(_FINISHED)
        graded = and_(finished, AttemptEvaluation.status == "SUCCEEDED")
        rows = await self._s.execute(
            select(
                Run.evaluation_id,
                func.count(),
                func.count().filter(graded),
                func.count().filter(graded, AttemptEvaluation.task_success.is_(True)),
                func.percentile_disc(0.95)
                .within_group(RunAttempt.duration_ms)
                .filter(finished, RunAttempt.duration_ms.is_not(None)),
            )
            .select_from(Run)
            .outerjoin(RunAttempt, _latest_attempt())
            .outerjoin(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
            .where(Run.evaluation_id.in_(evaluation_ids))
            .group_by(Run.evaluation_id)
        )
        return {
            eid: (int(n), int(g), int(p), _float(p95))
            for eid, n, g, p, p95 in rows.all()
            if eid is not None
        }

    async def usage(self, since: datetime) -> tuple[int, Decimal, int, int]:
        """(attempts, cost, tokens in, tokens out) over attempts finished since `since`."""
        row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(RunAttempt.cost_usd), 0),
                    cast(func.coalesce(func.sum(RunAttempt.tokens_in), 0), BigInteger),
                    cast(func.coalesce(func.sum(RunAttempt.tokens_out), 0), BigInteger),
                ).where(RunAttempt.finished_at >= since)
            )
        ).one()
        return int(row[0]), Decimal(str(row[1])), int(row[2]), int(row[3])

    async def graded(self, since: datetime) -> tuple[int, int]:
        """(graded, passed): COMPLETED/FAILED runs finished since `since` whose
        latest attempt's evaluation SUCCEEDED, and those with task_success."""
        latest = and_(RunAttempt.run_id == Run.id, RunAttempt.attempt_no == Run.attempt_count)
        row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.count().filter(AttemptEvaluation.task_success.is_(True)),
                )
                .select_from(Run)
                .join(RunAttempt, latest)
                .join(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
                .where(
                    Run.status.in_([RunStatus.COMPLETED, RunStatus.FAILED]),
                    Run.finished_at >= since,
                    AttemptEvaluation.status == "SUCCEEDED",
                )
            )
        ).one()
        return int(row[0]), int(row[1])
