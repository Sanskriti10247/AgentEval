"""Phase 5 Step 9: GET /runs, the run listing with keyset pagination (PROGRESS.md
section 21; F1 dead-letter view, F15). Rows are inserted with explicit created_at
values so ties, ordering and insertion between pages are exact."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.eval_seed import SM, agent_version, create_evaluation, frozen_dataset
from tests.integration.phase5_seed import insert_task

API = "/api/v1"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

RUN = """INSERT INTO runs (id, task_id, status, attempt_count, max_attempts, created_at,
    outcome, dead_letter_reason, finished_at)
    VALUES (:id, :task, :status, 0, 3, :at, :outcome, :dead, :fin)"""


async def add_task(engine: AsyncEngine) -> uuid.UUID:
    async with engine.begin() as conn:
        return await insert_task(conn)


async def add_runs(
    engine: AsyncEngine,
    task_id: uuid.UUID,
    stamps: list[datetime],
    *,
    status: str = "QUEUED",
    dead_letter: str | None = None,
) -> list[uuid.UUID]:
    failed = status == "FAILED"
    ids = [uuid.uuid4() for _ in stamps]
    async with engine.begin() as conn:
        for run_id, at in zip(ids, stamps, strict=True):
            await conn.execute(
                text(RUN),
                {
                    "id": run_id,
                    "task": task_id,
                    "status": status,
                    "at": at,
                    "outcome": "MODEL_ERROR" if failed else None,
                    "dead": dead_letter,
                    "fin": at if failed else None,
                },
            )
    return ids


async def walk(client: httpx.AsyncClient, limit: int, **params: str) -> list[list[str]]:
    """Every page of a listing, as lists of run ids, until next_after is null."""
    pages: list[list[str]] = []
    after: str | None = None
    while True:
        query = {**params, "limit": str(limit), **({"after": after} if after else {})}
        resp = await client.get(f"{API}/runs", params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"runs", "next_after"}
        pages.append([r["id"] for r in body["runs"]])
        after = body["next_after"]
        if after is None:
            return pages
        assert len(pages) < 100, "pagination did not terminate"


async def test_listing_is_newest_first_and_pages_without_duplicates_or_gaps(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    task = await add_task(engine)
    # 23 runs in 4 timestamps: groups of equal created_at force the id tiebreak.
    stamps = [T0 + timedelta(minutes=i // 6) for i in range(23)]
    ids = await add_runs(engine, task, stamps)
    expected = [
        str(i) for _, i in sorted(zip(stamps, ids, strict=True), key=lambda p: p, reverse=True)
    ]
    for limit in (1, 5, 7, 23, 24):
        pages = await walk(client, limit)
        flat = [i for page in pages for i in page]
        assert flat == expected, limit
        assert all(len(p) == limit for p in pages[:-1])
    pages = await walk(client, 23)
    assert len(pages) == 1  # exactly one full page: no empty trailing page, next_after null


async def test_pagination_is_stable_while_runs_are_inserted_between_pages(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    task = await add_task(engine)
    stamps = [T0 + timedelta(seconds=i // 3) for i in range(15)]
    original = await add_runs(engine, task, stamps)
    seen: list[str] = []

    resp = await client.get(f"{API}/runs", params={"limit": 4})
    body = resp.json()
    seen += [r["id"] for r in body["runs"]]
    while body["next_after"]:
        # New runs land (newest, and one older than the cursor) before every next page.
        await add_runs(engine, task, [datetime.now(UTC), T0 - timedelta(days=1)])
        resp = await client.get(f"{API}/runs", params={"limit": 4, "after": body["next_after"]})
        body = resp.json()
        seen += [r["id"] for r in body["runs"]]

    assert len(seen) == len(set(seen)), "a run was returned twice"
    assert {str(i) for i in original} <= set(seen), "an original run was skipped"
    # Newer runs inserted after the first page are never injected (the walk stays strictly
    # descending); the older ones inserted mid-walk are simply reached at the end.
    order = [(r_at, r_id) for r_id, r_at in await created_at(engine, seen)]
    assert order == sorted(order, reverse=True)


async def created_at(engine: AsyncEngine, ids: list[str]) -> list[tuple[str, datetime]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, created_at FROM runs WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        found = {str(r.id): r.created_at for r in rows}
    return [(i, found[i]) for i in ids]


async def test_filters_status_dead_letter_task_and_evaluation(
    client: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM
) -> None:
    task_a, task_b = await add_task(engine), await add_task(engine)
    queued = await add_runs(engine, task_a, [T0, T0 + timedelta(seconds=1)])
    dead = await add_runs(
        engine, task_b, [T0 + timedelta(seconds=2)], status="FAILED", dead_letter="non_retryable"
    )
    plain_failed = await add_runs(engine, task_b, [T0 + timedelta(seconds=3)], status="FAILED")

    async def listing(**params: str) -> set[str]:
        resp = await client.get(f"{API}/runs", params=params)
        assert resp.status_code == 200, resp.text
        return {r["id"] for r in resp.json()["runs"]}

    assert await listing(status="QUEUED") == {str(i) for i in queued}
    assert await listing(status="FAILED") == {str(i) for i in dead + plain_failed}
    assert await listing(dead_letter="true") == {str(i) for i in dead}
    assert await listing(dead_letter="false") == {str(i) for i in queued + plain_failed}
    assert await listing(status="FAILED", dead_letter="true") == {str(i) for i in dead}
    assert await listing(status="QUEUED", dead_letter="true") == set()
    assert await listing(task_id=str(task_a)) == {str(i) for i in queued}
    assert await listing(task_id=str(task_b), status="FAILED", dead_letter="true") == {str(dead[0])}
    assert await listing(task_id=str(uuid.uuid4())) == set()

    ds, _tasks = await frozen_dataset(client)
    version = await agent_version(sessionmaker)
    batch = (await create_evaluation(client, ds, version, repetitions=2)).json()["id"]
    in_batch = await listing(evaluation_id=batch)
    assert len(in_batch) == 6 and not in_batch & {str(i) for i in queued + dead + plain_failed}
    assert await listing(evaluation_id=str(uuid.uuid4())) == set()
    assert len(await listing()) == 6 + 4  # no filter: everything


async def test_dead_letter_view_is_served_by_the_failed_index(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """F1: the DLQ listing is `status = 'FAILED' AND dead_letter_reason IS NOT NULL`, which
    the partial index ix_runs_failed can serve (forced here, as the table is tiny)."""
    task = await add_task(engine)
    await add_runs(engine, task, [T0], status="FAILED", dead_letter="retries_exhausted")
    async with engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            r[0]
            for r in await conn.execute(
                text(
                    "EXPLAIN SELECT * FROM runs WHERE status = 'FAILED' AND dead_letter_reason IS"
                    " NOT NULL ORDER BY created_at DESC, id DESC LIMIT 51"
                )
            )
        )
    assert "ix_runs_failed" in plan, plan


async def test_response_shape_and_the_run_fields_added_in_step_9(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    task = await add_task(engine)
    [dead] = await add_runs(engine, task, [T0], status="FAILED", dead_letter="retries_exhausted")
    body = (await client.get(f"{API}/runs", params={"dead_letter": "true"})).json()
    assert body["next_after"] is None
    [run] = body["runs"]
    assert run["id"] == str(dead)
    assert (run["status"], run["outcome"], run["max_attempts"], run["replay_count"]) == (
        "FAILED", "MODEL_ERROR", 3, 0,
    )  # fmt: skip
    assert run["dead_letter_reason"] == "retries_exhausted"
    assert run["failure_reason"] is None and run["next_attempt_at"] is None
    assert (await client.get(f"{API}/runs")).json()["runs"] == body["runs"]  # default limit
    assert (await client.get(f"{API}/runs/{dead}")).json() == run


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "x"},
        {"status": "DEAD_LETTER"},  # F1: not a public status
        {"status": "queued"},
        {"dead_letter": "maybe"},
        {"evaluation_id": "nope"},
        {"task_id": "nope"},
        {"after": "garbage"},
        {"after": "2026-01-01T00:00:00Z|not-a-uuid"},
        {"after": "not-a-time|" + str(uuid.uuid4())},
        {"after": "2026-01-01T00:00:00|" + str(uuid.uuid4())},  # naive timestamp
    ],
)
async def test_bad_query_parameters_are_422(
    client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    resp = await client.get(f"{API}/runs", params=params)
    assert resp.status_code == 422, resp.text


async def test_the_largest_page_is_200_and_an_empty_table_is_an_empty_page(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    empty = (await client.get(f"{API}/runs", params={"limit": 200})).json()
    assert empty == {"runs": [], "next_after": None}
    task = await add_task(engine)
    await add_runs(engine, task, [T0 + timedelta(seconds=i) for i in range(201)])
    first = (await client.get(f"{API}/runs", params={"limit": 200})).json()
    assert len(first["runs"]) == 200 and first["next_after"] is not None
    assert "+" not in first["next_after"]  # survives an unencoded query string
    rest = (
        await client.get(f"{API}/runs", params={"limit": 200, "after": first["next_after"]})
    ).json()
    assert len(rest["runs"]) == 1 and rest["next_after"] is None
