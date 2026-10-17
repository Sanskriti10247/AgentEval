"""Dispatch bookkeeping shared by the submit and batch paths."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.logging import get_logger
from agenteval_core.repositories.runs import RunRepository

log = get_logger(__name__)


async def stamp_dispatched(session: AsyncSession, run_ids: Sequence[uuid.UUID]) -> None:
    """After a successful XADD, record `dispatched_at` so the reconciler does
    not redispatch a run that is merely waiting its turn.

    Best effort: the message is already in the stream, so a failure here must
    not fail the request. The worst case is a duplicate message after
    `redispatch_after_s`, which the conditional claim makes harmless. The
    session's objects are detached before the rollback so the caller can still
    read the rows it already loaded (a rollback would expire them).
    """
    try:
        await RunRepository(session).mark_dispatched(run_ids)
        await session.commit()
    except Exception:
        log.exception("dispatch_stamp_failed", runs=len(run_ids))
        session.expunge_all()
        try:
            await session.rollback()
        except Exception:
            log.exception("dispatch_stamp_rollback_failed")
