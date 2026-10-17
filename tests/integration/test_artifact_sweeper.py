"""Reconciler step 5 (ADR-008 E): the artifact sweeper against real Postgres
and a real artifact directory. It deletes files no row references once they
are older than the grace period (by mtime, `.tmp` files included), never a
referenced file, never a young one. No Docker, no Redis traffic."""

import os
import time
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.policy import RECONCILER_LOCK_NS
from agenteval_core.repositories.recovery import STEP_ARTIFACTS
from agenteval_worker.reconciler import Reconciler
from tests.integration.phase5_seed import add_artifact, running_run

SM = async_sessionmaker[AsyncSession]
GRACE = 120.0
OLD = GRACE + 60  # seconds of age
YOUNG = GRACE - 60


def reconciler(
    sm: SM,
    queue: RunQueue,
    store: LocalArtifactStore | None,
    *,
    clock: list[float] | None = None,
    interval_s: float = 300.0,
) -> Reconciler:
    now = clock if clock is not None else [0.0]
    return Reconciler(
        sm,
        queue,
        "w-sweep",
        interval_s=0.05,
        lease_ttl_s=30.0,
        redispatch_after_s=60.0,
        orphan_sweep_interval_s=interval_s,
        kill_attempt=lambda a: 0,
        sweep_orphans=lambda: 0,
        clock=lambda: now[0],
        artifact_store=store,
        artifact_sweep_grace_s=GRACE,
    )


def plant(
    store: LocalArtifactStore, name: str, age_s: float, attempt: uuid.UUID | None = None
) -> str:
    """A file of the given age under the store; returns its key."""
    directory = store.root / str(attempt or uuid.uuid4())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"bytes")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path.relative_to(store.root).as_posix()


async def keys(store: LocalArtifactStore) -> set[str]:
    return {k for k, _ in await store.list_files()}


async def test_an_old_orphan_is_deleted(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    orphan = plant(artifact_store, f"workspace-{'a' * 64}.tar.gz", OLD)
    removed = await reconciler(sessionmaker, queue, artifact_store).step_artifact_sweep(force=True)
    assert removed == 1 and await keys(artifact_store) == set()
    assert orphan not in await keys(artifact_store)


async def test_an_old_tmp_file_is_deleted(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    plant(artifact_store, f"workspace-{'b' * 64}.tar.gz.1f2e3d4c.tmp", OLD)
    assert (
        await reconciler(sessionmaker, queue, artifact_store).step_artifact_sweep(force=True) == 1
    )
    assert await keys(artifact_store) == set()


async def test_young_files_are_kept_even_when_unreferenced(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """A writer inside its deadline has a file and no row yet."""
    young = plant(artifact_store, f"workspace-{'c' * 64}.tar.gz", YOUNG)
    young_tmp = plant(artifact_store, f"workspace-{'d' * 64}.tar.gz.aa.tmp", 1)
    removed = await reconciler(sessionmaker, queue, artifact_store).step_artifact_sweep(force=True)
    assert removed == 0
    assert await keys(artifact_store) == {young, young_tmp}


async def test_a_referenced_file_is_never_deleted_however_old(
    engine: AsyncEngine, sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    _, attempt = await running_run(engine)
    name = f"workspace-{'e' * 64}.tar.gz"
    referenced = plant(artifact_store, name, 10 * OLD, attempt)
    await add_artifact(engine, attempt, storage_key=referenced)
    orphan = plant(artifact_store, f"workspace-{'f' * 64}.tar.gz", OLD, attempt)  # same dir

    removed = await reconciler(sessionmaker, queue, artifact_store).step_artifact_sweep(force=True)

    assert removed == 1
    assert await keys(artifact_store) == {referenced}
    assert orphan not in await keys(artifact_store)


async def test_a_file_referenced_only_after_the_listing_is_kept(
    engine: AsyncEngine, sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    """List first, read references second: a row committed between the two is
    seen, so its (already listed) file is not deleted."""
    _, attempt = await running_run(engine)
    key = plant(artifact_store, f"workspace-{'9' * 64}.tar.gz", OLD, attempt)

    class RowAppearsAfterListing(LocalArtifactStore):
        inserted = False

        async def list_files(self) -> list[tuple[str, float]]:
            listed = await super().list_files()
            if not self.inserted:
                self.inserted = True
                await add_artifact(engine, attempt, storage_key=key)
            return listed

    store = RowAppearsAfterListing(artifact_store.root)
    assert await reconciler(sessionmaker, queue, store).step_artifact_sweep(force=True) == 0
    assert await keys(store) == {key}


async def test_a_legacy_content_row_references_nothing_on_the_volume(
    engine: AsyncEngine, sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    _, attempt = await running_run(engine)
    await add_artifact(engine, attempt, storage_key=None)  # Phase 4: bytes in the row
    orphan = plant(artifact_store, f"workspace-{'1' * 64}.tar.gz", OLD, attempt)
    assert (
        await reconciler(sessionmaker, queue, artifact_store).step_artifact_sweep(force=True) == 1
    )
    assert orphan not in await keys(artifact_store)


async def test_the_sweeper_has_its_own_timer(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    clock = [1000.0]
    rec = reconciler(sessionmaker, queue, artifact_store, clock=clock, interval_s=300.0)
    plant(artifact_store, f"workspace-{'2' * 64}.tar.gz", OLD)

    assert await rec.step_artifact_sweep() is None  # not due: one interval from start
    assert len(await keys(artifact_store)) == 1
    clock[0] += 299
    assert await rec.step_artifact_sweep() is None
    clock[0] += 2
    assert await rec.step_artifact_sweep() == 1  # due
    assert await keys(artifact_store) == set()
    plant(artifact_store, f"workspace-{'3' * 64}.tar.gz", OLD)
    assert await rec.step_artifact_sweep() is None  # the timer restarted


async def test_tick_runs_the_sweep_when_due_and_not_before(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    clock = [0.0]
    rec = reconciler(sessionmaker, queue, artifact_store, clock=clock, interval_s=300.0)
    plant(artifact_store, f"workspace-{'4' * 64}.tar.gz", OLD)
    await rec.tick()
    assert len(await keys(artifact_store)) == 1
    clock[0] = 301.0
    await rec.tick()
    assert await keys(artifact_store) == set()


async def test_another_reconcilers_advisory_lock_skips_the_step(
    engine: AsyncEngine, sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    plant(artifact_store, f"workspace-{'5' * 64}.tar.gz", OLD)
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT pg_advisory_lock(:ns, :step)"),
            {"ns": RECONCILER_LOCK_NS, "step": STEP_ARTIFACTS},
        )
        try:
            skipped = await reconciler(sessionmaker, queue, artifact_store).step_artifact_sweep(
                force=True
            )
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:ns, :step)"),
                {"ns": RECONCILER_LOCK_NS, "step": STEP_ARTIFACTS},
            )
    assert skipped is None and len(await keys(artifact_store)) == 1


async def test_no_store_means_no_sweep(sessionmaker: SM, queue: RunQueue) -> None:
    assert await reconciler(sessionmaker, queue, None).step_artifact_sweep(force=True) is None


async def test_a_failing_delete_does_not_stop_the_rest(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    bad = plant(artifact_store, f"workspace-{'6' * 64}.tar.gz", OLD)
    plant(artifact_store, f"workspace-{'7' * 64}.tar.gz", OLD)

    class Flaky(LocalArtifactStore):
        async def delete(self, key: str) -> None:
            if key == bad:
                raise OSError("read-only")
            await super().delete(key)

    store = Flaky(artifact_store.root)
    assert await reconciler(sessionmaker, queue, store).step_artifact_sweep(force=True) == 1
    assert await keys(store) == {bad}


async def test_an_empty_or_missing_root_is_fine(
    sessionmaker: SM, queue: RunQueue, tmp_path: Path
) -> None:
    store = LocalArtifactStore(tmp_path / "fresh")
    assert await reconciler(sessionmaker, queue, store).step_artifact_sweep(force=True) == 0
