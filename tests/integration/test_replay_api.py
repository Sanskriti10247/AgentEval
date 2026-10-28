"""POST /runs/{id}/replay (R14, F10, G4).

DB-level cases use hand-seeded FAILED runs so every response code and every
cleared column is hit exactly; the last test drives the real worker: a run that
failed with a retryable model error is replayed and completes as attempt 2.
"""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue
from tests.integration.phase5_seed import running_run
from tests.integration.test_agent_executor import (
    FIX_THE_BUG,
    executor,
    handle_next,
    load,
    make_agent_run,
)
from tests.integration.test_reconciler import run_row, streamed

SM = async_sessionmaker[AsyncSession]
API = "/api/v1"
HIDDEN = (
    "lease_epoch",
    "lease_expires_at",
    "heartbeat_at",
    "dispatched_at",
    "unstarted_claims",
    "workspace_capture",
    "storage_key",
)


async def failed_run(
    engine: AsyncEngine,
    *,
    max_attempts: int = 3,
    dead_letter: str | None = "retries_exhausted",
    unstarted_claims: int = 0,
) -> uuid.UUID:
    """A FAILED run after one retryable model failure, as R10 leaves it: outcome,
    error message and dead-letter reason set, attempt 1 FINISHED, dispatched."""
    run_id, attempt_id = await running_run(engine, max_attempts=max_attempts)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """UPDATE run_attempts SET status = 'FINISHED', outcome = 'MODEL_ERROR',
                     failure_reason = 'model_overloaded', retryable = true, finished_at = now()
                   WHERE id = :a"""
            ),
            {"a": attempt_id},
        )
        await conn.execute(
            text(
                """UPDATE runs SET status = 'FAILED', outcome = 'MODEL_ERROR',
                     error_message = 'model failed: overloaded', finished_at = now(),
                     dead_letter_reason = :d, unstarted_claims = :u, dispatched_at = now(),
                     lease_expires_at = NULL, heartbeat_at = NULL WHERE id = :i"""
            ),
            {"i": run_id, "d": dead_letter, "u": unstarted_claims},
        )
    return run_id


async def replay(
    client: httpx.AsyncClient,
    run_id: uuid.UUID | str,
    *,
    additional: int | None = None,
    key: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"{API}/runs/{run_id}/replay",
        json=None if additional is None else {"additional_attempts": additional},
        headers={"Idempotency-Key": key} if key else {},
    )


async def replay_rows(engine: AsyncEngine, run_id: uuid.UUID) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            await conn.execute(
                text(
                    "SELECT replay_no, additional_attempts, idempotency_key FROM run_replays"
                    " WHERE run_id = :r ORDER BY replay_no"
                ),
                {"r": run_id},
            )
        )


# -- 202: the transition -------------------------------------------------------------------------


async def test_r14_requeues_a_failed_run_and_clears_what_the_failure_wrote(
    client: httpx.AsyncClient, engine: AsyncEngine, redis: Redis, settings: Settings
) -> None:
    run_id = await failed_run(engine, unstarted_claims=2)
    before = await run_row(engine, run_id)

    resp = await replay(client, run_id)  # no body: additional_attempts defaults to 1
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["id"] == str(run_id) and body["status"] == "QUEUED"
    assert body["max_attempts"] == 4 and body["replay_count"] == 1
    assert body["attempt_count"] == 1  # NOT reset: attempt numbers continue
    for cleared in ("outcome", "error_message", "dead_letter_reason", "failure_reason"):
        assert body[cleared] is None, cleared
    assert body["finished_at"] is None and body["next_attempt_at"] is None
    assert not any(key in resp.text for key in HIDDEN)

    after = await run_row(engine, run_id)
    assert (after.unstarted_claims, after.cancel_requested_at, after.lease_expires_at) == (
        0,
        None,
        None,
    )
    assert before.dispatched_at is not None
    assert (
        after.dispatched_at is not None and after.dispatched_at > before.dispatched_at
    )  # re-stamped
    assert await streamed(redis, settings) == [run_id]  # published after the commit
    [row] = await replay_rows(engine, run_id)
    assert (row.replay_no, row.additional_attempts, row.idempotency_key) == (1, 1, None)
    async with engine.connect() as conn:  # the attempt's history is untouched
        attempts = (
            await conn.execute(
                text("SELECT attempt_no, status, outcome FROM run_attempts WHERE run_id = :r"),
                {"r": run_id},
            )
        ).all()
    assert [tuple(a) for a in attempts] == [(1, "FINISHED", "MODEL_ERROR")]


@pytest.mark.parametrize(("additional", "expected"), [(1, 4), (3, 6), (7, 10)])
async def test_additional_attempts_grow_the_budget(
    client: httpx.AsyncClient, engine: AsyncEngine, additional: int, expected: int
) -> None:
    run_id = await failed_run(engine)
    resp = await replay(client, run_id, additional=additional)
    assert resp.status_code == 202, resp.text
    assert resp.json()["max_attempts"] == expected


async def test_a_run_that_failed_without_an_attempt_can_be_replayed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """R4b leaves a FAILED run with failure_reason claim_abandoned and no attempt."""
    run_id = await failed_run(engine, unstarted_claims=3)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_attempts WHERE run_id = :r"), {"r": run_id})
        await conn.execute(
            text(
                "UPDATE runs SET outcome = 'INTERNAL_ERROR', attempt_count = 0,"
                " failure_reason = 'claim_abandoned' WHERE id = :r"
            ),
            {"r": run_id},
        )
    resp = await replay(client, run_id, additional=2)
    assert resp.status_code == 202, resp.text
    assert (resp.json()["failure_reason"], resp.json()["attempt_count"]) == (None, 0)
    assert (await run_row(engine, run_id)).unstarted_claims == 0


# -- 422, 409, 404 -------------------------------------------------------------------------------


@pytest.mark.parametrize(("max_attempts", "additional"), [(10, 1), (3, 8), (9, 2), (2, 9)])
async def test_over_the_cap_is_422_and_writes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, max_attempts: int, additional: int
) -> None:
    run_id = await failed_run(engine, max_attempts=max_attempts)
    # 2 + 9 = 11 > 10 and 3 + 8 = 11 > 10; the cap is on the total.
    resp = await replay(client, run_id, additional=additional, key="cap")
    assert resp.status_code == 422, resp.text
    after = await run_row(engine, run_id)
    assert (after.status, after.max_attempts, after.replay_count) == ("FAILED", max_attempts, 0)
    assert await replay_rows(engine, run_id) == []  # not even the key was spent


@pytest.mark.parametrize("additional", [0, 10, -1])
async def test_additional_attempts_outside_1_to_9_is_422(
    client: httpx.AsyncClient, engine: AsyncEngine, additional: int
) -> None:
    run_id = await failed_run(engine)
    assert (await replay(client, run_id, additional=additional)).status_code == 422
    resp = await client.post(f"{API}/runs/{run_id}/replay", json={"extra": 1})
    assert resp.status_code == 422  # unknown fields are refused
    assert (await run_row(engine, run_id)).status == "FAILED"


@pytest.mark.parametrize("status", ["COMPLETED", "QUEUED", "CANCELLED", "RUNNING", "RETRYING"])
async def test_only_a_failed_run_can_be_replayed(
    client: httpx.AsyncClient, engine: AsyncEngine, status: str
) -> None:
    run_id, _ = await running_run(engine)
    sql = {
        "COMPLETED": "status = 'COMPLETED', outcome = 'AGENT_FINISHED', finished_at = now(),"
        " lease_expires_at = NULL",
        "QUEUED": "status = 'QUEUED', lease_expires_at = NULL",
        "CANCELLED": "status = 'CANCELLED', cancel_requested_at = now(), cancel_reason = 'user',"
        " finished_at = now(), lease_expires_at = NULL",
        "RUNNING": "status = 'RUNNING'",
        "RETRYING": "status = 'RETRYING', next_attempt_at = now() + interval '1 h',"
        " lease_expires_at = NULL",
    }[status]
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE runs SET {sql} WHERE id = :i"), {"i": run_id})
    before = await run_row(engine, run_id)

    resp = await replay(client, run_id)
    assert resp.status_code == 409, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    after = await run_row(engine, run_id)
    assert (after.status, after.max_attempts, after.replay_count) == (
        before.status, before.max_attempts, 0,
    )  # fmt: skip
    assert await replay_rows(engine, run_id) == []


async def test_an_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    resp = await replay(client, uuid.uuid4())
    assert resp.status_code == 404
    assert (await client.post(f"{API}/runs/not-a-uuid/replay")).status_code == 422


# -- G4: idempotency -----------------------------------------------------------------------------


async def test_g4_the_same_key_repeated_is_200_and_changes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, redis: Redis, settings: Settings
) -> None:
    run_id = await failed_run(engine)
    first = await replay(client, run_id, additional=2, key="k-1")
    assert first.status_code == 202
    after_first = await run_row(engine, run_id)
    stream_len = len(await streamed(redis, settings))

    again = await replay(client, run_id, additional=2, key="k-1")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    after_again = await run_row(engine, run_id)
    assert (after_again.max_attempts, after_again.replay_count) == (5, 1)
    assert after_again.dispatched_at == after_first.dispatched_at
    assert len(await streamed(redis, settings)) == stream_len == 1  # not published twice
    assert len(await replay_rows(engine, run_id)) == 1


async def test_g4_a_repeat_stays_200_after_the_run_moved_on(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The retry arrives after a worker claimed (or even failed) the replayed run: the
    key is what answers, not the run's current state."""
    run_id = await failed_run(engine)
    assert (await replay(client, run_id, key="k-late")).status_code == 202
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runs SET status = 'CLAIMED', worker_id = 'w', lease_epoch = 2,"
                " lease_expires_at = now() + interval '30 s', heartbeat_at = now() WHERE id = :i"
            ),
            {"i": run_id},
        )
    again = await replay(client, run_id, key="k-late")
    assert again.status_code == 200 and again.json()["status"] == "CLAIMED"
    assert (await run_row(engine, run_id)).replay_count == 1


async def test_g4_a_key_used_for_another_run_or_another_body_is_409(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    one, other = await failed_run(engine), await failed_run(engine)
    assert (await replay(client, one, additional=2, key="shared")).status_code == 202

    other_run = await replay(client, other, additional=2, key="shared")
    assert other_run.status_code == 409 and "Idempotency-Key" in other_run.json()["detail"]
    different_body = await replay(client, one, additional=3, key="shared")
    assert different_body.status_code == 409

    assert (await run_row(engine, other)).status == "FAILED"  # the loser wrote nothing
    assert await replay_rows(engine, other) == []
    assert (await run_row(engine, one)).max_attempts == 5


async def test_a_new_key_replays_again_after_the_run_failed_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = await failed_run(engine)
    assert (await replay(client, run_id, key="a")).status_code == 202
    # A different key while the run is QUEUED again: it is not FAILED.
    assert (await replay(client, run_id, key="b")).status_code == 409

    async with engine.begin() as conn:  # attempt 2 failed too (as a worker would leave it)
        await conn.execute(
            text(
                "UPDATE runs SET status = 'FAILED', outcome = 'MODEL_ERROR', attempt_count = 4,"
                " finished_at = now(), dead_letter_reason = 'retries_exhausted' WHERE id = :i"
            ),
            {"i": run_id},
        )
    second = await replay(client, run_id, additional=2, key="b")
    assert second.status_code == 202, second.text
    assert (second.json()["replay_count"], second.json()["max_attempts"]) == (2, 6)
    assert second.json()["attempt_count"] == 4
    assert [(r.replay_no, r.idempotency_key) for r in await replay_rows(engine, run_id)] == [
        (1, "a"), (2, "b"),
    ]  # fmt: skip


async def test_g4_concurrent_requests_with_one_key_replay_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = await failed_run(engine)
    responses = await asyncio.gather(*(replay(client, run_id, key="race") for _ in range(4)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 200, 202], codes
    after = await run_row(engine, run_id)
    assert (after.status, after.max_attempts, after.replay_count) == ("QUEUED", 4, 1)
    assert len(await replay_rows(engine, run_id)) == 1


async def test_concurrent_requests_with_different_keys_replay_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = await failed_run(engine)
    responses = await asyncio.gather(*(replay(client, run_id, key=f"k{i}") for i in range(4)))
    assert sorted(r.status_code for r in responses) == [202, 409, 409, 409]
    after = await run_row(engine, run_id)
    assert (after.max_attempts, after.replay_count) == (4, 1)


async def test_a_key_raced_in_for_another_run_loses_cleanly(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    one, two = await failed_run(engine), await failed_run(engine)
    responses = await asyncio.gather(
        replay(client, one, key="same"), replay(client, two, key="same")
    )
    assert sorted(r.status_code for r in responses) == [202, 409]
    statuses = {(await run_row(engine, r)).status for r in (one, two)}
    assert statuses == {"QUEUED", "FAILED"}  # the loser rolled back entirely


# -- dispatch ------------------------------------------------------------------------------------


async def test_a_failed_publish_is_503_and_the_same_key_dispatches_it(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    redis: Redis,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await failed_run(engine)
    queue: RunQueue = client.app.state.queue  # type: ignore[attr-defined]
    real_publish = queue.publish

    async def broken(item_id: uuid.UUID) -> str:
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(queue, "publish", broken)
    resp = await replay(client, run_id, key="retry-me")
    assert resp.status_code == 503 and resp.headers["retry-after"] == "1"
    after = await run_row(engine, run_id)  # committed and QUEUED, never stamped
    assert (after.status, after.replay_count, after.dispatched_at) == ("QUEUED", 1, None)

    monkeypatch.setattr(queue, "publish", real_publish)
    again = await replay(client, run_id, key="retry-me")
    assert again.status_code == 200 and again.json()["status"] == "QUEUED"
    assert await streamed(redis, settings) == [run_id]
    assert (await run_row(engine, run_id)).dispatched_at is not None
    assert (await run_row(engine, run_id)).replay_count == 1


# -- the real worker -----------------------------------------------------------------------------


@pytest.mark.docker
async def test_a_replayed_run_completes_as_the_next_attempt(
    client: httpx.AsyncClient, sessionmaker: SM, queue: RunQueue, engine: AsyncEngine
) -> None:
    """FAILED after a retryable failure (max_attempts 1) -> replay -> QUEUED -> a worker
    claims it and finishes it as attempt 2; attempt 1 is kept."""
    always_overloaded = [{"type": "error", "kind": "overloaded"} for _ in range(4)]
    run_id = await make_agent_run(sessionmaker, queue, always_overloaded, max_attempts=1)
    ex = executor(sessionmaker, queue)
    await handle_next(ex, queue)
    run, [first], _ = await load(sessionmaker, run_id)
    assert (run.status, run.dead_letter_reason) == ("FAILED", "retries_exhausted")
    assert (first.attempt_no, first.failure_reason, first.retryable) == (
        1,
        "model_overloaded",
        True,
    )

    async with engine.begin() as conn:  # the operator "fixes" the cause: the model works now
        await conn.execute(
            text(
                "UPDATE agent_versions SET model_settings = CAST(:s AS jsonb)"
                " WHERE id = (SELECT agent_version_id FROM runs WHERE id = :r)"
            ),
            {"s": json.dumps({"script": FIX_THE_BUG}), "r": run_id},
        )

    resp = await replay(client, run_id, additional=1, key="fixed")
    assert resp.status_code == 202, resp.text
    assert (resp.json()["status"], resp.json()["max_attempts"]) == ("QUEUED", 2)

    await handle_next(ex, queue)  # the replayed message
    run, attempts, _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, run.dead_letter_reason) == (
        "COMPLETED",
        "AGENT_FINISHED",
        None,
    )
    assert [(a.attempt_no, a.status, a.outcome) for a in attempts] == [
        (1, "FINISHED", "MODEL_ERROR"),
        (2, "FINISHED", "AGENT_FINISHED"),
    ]
    assert (run.attempt_count, run.max_attempts, run.replay_count) == (2, 2, 1)
    got = (await client.get(f"{API}/runs/{run_id}")).json()
    assert (got["status"], got["replay_count"], got["attempt_count"]) == ("COMPLETED", 1, 2)
