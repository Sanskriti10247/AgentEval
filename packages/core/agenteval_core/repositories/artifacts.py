"""T-artifact (ADR-008 A and E): save an attempt's workspace snapshot.

The bytes go to the ArtifactStore first, with no database lock held. Only then
does ONE fenced statement insert the metadata row:

    INSERT INTO attempt_artifacts ...
    SELECT ... FROM (SELECT id FROM run_attempts
                      WHERE id = :a AND status = 'RUNNING' AND lease_epoch = :e FOR SHARE) a
     WHERE NOT EXISTS (SELECT 1 FROM runs WHERE id = :run AND cancel_requested_at IS NOT NULL)
    ON CONFLICT (attempt_id, kind) DO NOTHING

It locks only the attempt row (never `runs`), under `SET LOCAL statement_timeout`.
Zero rows means one of three things: the attempt already has an artifact (the
same SHA-256 is success - a retry), or it has a different one (never
overwritten), or the statement was fenced (the attempt is no longer RUNNING at
our epoch, or a cancel is pending). In every case but the first, our file has
no row and is removed here; the sweeper is the backstop if the removal fails.

A save failure never changes the attempt's outcome: the caller records
`workspace_capture = 'persist_failed'` and finishes normally.

Like every repository this flushes but never commits. The caller commits when
the result is STORED and otherwise rolls back (a statement timeout leaves the
transaction aborted).
"""

import json
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.artifacts import ArtifactStore, StoredArtifact
from agenteval_core.evaluation.taxonomy import ArtifactKind
from agenteval_core.logging import get_logger
from agenteval_core.models.base import new_id
from agenteval_core.observability import tracing

log = get_logger(__name__)

_INSERT = text(
    """
    INSERT INTO attempt_artifacts (id, attempt_id, kind, storage_key, size_bytes, sha256, metadata)
    SELECT CAST(:id AS uuid), a.id, CAST(:kind AS text), CAST(:key AS text),
           CAST(:size AS integer), CAST(:sha AS text), CAST(:meta AS jsonb)
      FROM (SELECT id FROM run_attempts
             WHERE id = :attempt AND status = 'RUNNING' AND lease_epoch = :epoch
               FOR SHARE) a
     WHERE NOT EXISTS (SELECT 1 FROM runs WHERE id = :run AND cancel_requested_at IS NOT NULL)
    ON CONFLICT (attempt_id, kind) DO NOTHING
    RETURNING id
    """
)
_EXISTING = text(
    "SELECT sha256 FROM attempt_artifacts WHERE attempt_id = :attempt AND kind = :kind"
)


class SaveResult(StrEnum):
    STORED = "stored"  # a row exists for this content: commit
    FENCED = "fenced"  # fenced, cancelled, or different content already stored: no row of ours
    FAILED = "failed"  # file write, deadline, timeout or database error: no row of ours


class ArtifactRepository:
    def __init__(self, session: AsyncSession, store: ArtifactStore) -> None:
        self._s = session
        self._store = store

    async def save(
        self,
        *,
        run_id: uuid.UUID,
        attempt_id: uuid.UUID,
        epoch: int,
        data: bytes,
        metadata: dict[str, Any],
        insert_timeout_s: float,
        deadline_s: float,
        kind: ArtifactKind = ArtifactKind.WORKSPACE,
        clock: Callable[[], float] = time.monotonic,
    ) -> SaveResult:
        with tracing.span("artifact.save", attempt_id=attempt_id, size_bytes=len(data)) as sp:
            result = await self._save(
                run_id=run_id,
                attempt_id=attempt_id,
                epoch=epoch,
                data=data,
                metadata=metadata,
                insert_timeout_s=insert_timeout_s,
                deadline_s=deadline_s,
                kind=kind,
                clock=clock,
            )
            sp.set(result=result.value)
            return result

    async def _save(
        self,
        *,
        run_id: uuid.UUID,
        attempt_id: uuid.UUID,
        epoch: int,
        data: bytes,
        metadata: dict[str, Any],
        insert_timeout_s: float,
        deadline_s: float,
        kind: ArtifactKind,
        clock: Callable[[], float],
    ) -> SaveResult:
        started = time.monotonic()
        try:
            stored = await self._store.put(attempt_id, kind, data)
        except Exception as exc:
            log.warning("artifact_write_failed", attempt_id=str(attempt_id), error=str(exc))
            return SaveResult.FAILED

        # E: the sweeper's grace outlasts deadline + statement timeout, so a row
        # inserted within the deadline can never point at a swept file.
        if clock() - stored.renamed_at > deadline_s:
            log.warning("artifact_deadline_missed", attempt_id=str(attempt_id), key=stored.key)
            await self._remove(stored)
            return SaveResult.FAILED

        try:
            result = await self._insert(
                stored, run_id, attempt_id, epoch, kind, metadata, insert_timeout_s
            )
        except Exception as exc:
            log.warning(
                "artifact_insert_failed",
                attempt_id=str(attempt_id),
                error_type=type(exc).__name__,
            )
            await self._remove(stored)
            return SaveResult.FAILED
        if result is not SaveResult.STORED:
            await self._remove(stored)
        log.info(
            "artifact_save",
            attempt_id=str(attempt_id),
            result=result,
            size_bytes=stored.size,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    async def _insert(
        self,
        stored: StoredArtifact,
        run_id: uuid.UUID,
        attempt_id: uuid.UUID,
        epoch: int,
        kind: str,
        metadata: dict[str, Any],
        insert_timeout_s: float,
    ) -> SaveResult:
        await self._s.execute(
            text(f"SET LOCAL statement_timeout = '{max(1, int(insert_timeout_s * 1000))}ms'")
        )
        inserted = (
            await self._s.execute(
                _INSERT,
                {
                    "id": new_id(),
                    "kind": str(kind),
                    "key": stored.key,
                    "size": stored.size,
                    "sha": stored.sha256,
                    "meta": json.dumps(metadata),
                    "attempt": attempt_id,
                    "epoch": epoch,
                    "run": run_id,
                },
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return SaveResult.STORED
        existing = (
            await self._s.execute(_EXISTING, {"attempt": attempt_id, "kind": str(kind)})
        ).scalar_one_or_none()
        if existing == stored.sha256:
            return SaveResult.STORED  # a retry of an earlier save: same file, same row
        return SaveResult.FENCED

    async def _remove(self, stored: StoredArtifact) -> None:
        if stored.existed:
            return  # possibly referenced by an earlier save's row; the sweeper decides
        try:
            await self._store.delete(stored.key)
        except Exception as exc:  # the sweeper is the backstop
            log.warning("artifact_remove_failed", key=stored.key, error=str(exc))
