"""Phase 6 step 3: the dashboard's read API (ADR-010 C) - paged lists for
agents, tasks, datasets, evaluations and workers, GET /stats/overview, CORS.

Rows are inserted with explicit created_at values (groups of equal stamps force
the id tiebreak), so ordering, paging and filters have exact expected answers.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agenteval_api.main import create_app
from agenteval_core.config import Settings
from tests.integration.eval_seed import SM, agent_version, create_evaluation, frozen_dataset, seed

API = "/api/v1"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def walk(client: httpx.AsyncClient, path: str, key: str, limit: int, **params: Any) -> Any:
    """Every page of a listing: a list of pages of item ids, until next_after is null."""
    pages: list[list[Any]] = []
    after: str | None = None
    while True:
        query = {**params, "limit": str(limit), **({"after": after} if after else {})}
        resp = await client.get(f"{API}{path}", params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {key, "next_after"}
        assert len(body[key]) <= limit
        pages.append([item["id"] for item in body[key]])
        after = body["next_after"]
        if after is None:
            return pages
        assert len(pages) < 100, "pagination did not terminate"


def flat(pages: list[list[Any]]) -> list[Any]:
    return [i for page in pages for i in page]


def stamps(n: int, per_stamp: int = 4) -> list[datetime]:
    return [T0 + timedelta(minutes=i // per_stamp) for i in range(n)]


def newest_first(rows: list[tuple[datetime, uuid.UUID]]) -> list[str]:
    return [str(i) for _, i in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True)]


# -- agents ------------------------------------------------------------------------------------


async def add_agents(engine: AsyncEngine, n: int) -> list[tuple[datetime, uuid.UUID]]:
    rows = []
    async with engine.begin() as conn:
        for i, at in enumerate(stamps(n)):
            agent_id = uuid.uuid4()
            rows.append((at, agent_id))
            await conn.execute(
                text("INSERT INTO agents (id, name, created_at) VALUES (:id, :n, :at)"),
                {"id": agent_id, "n": f"agent-{i}", "at": at},
            )
    return rows


async def add_version(engine: AsyncEngine, agent_id: uuid.UUID, number: int, model: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO agent_versions (id, agent_id, version, model_provider, model,
                     system_prompt, tools, shell_allowlist, limits, model_settings, config_hash)
                   VALUES (gen_random_uuid(), :a, :v, 'fake', :m, 'SECRET-PROMPT',
                     '["read_file","run_tests"]', '[]', '{}', '{}', repeat(:h, 64))"""
            ),
            {"a": agent_id, "v": number, "m": model, "h": str(number)},
        )


async def test_agents_page_newest_first_without_duplicates_or_gaps(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    rows = await add_agents(engine, 11)
    expected = newest_first(rows)
    assert flat(await walk(client, "/agents", "agents", 3)) == expected
    pages = await walk(client, "/agents", "agents", 4)
    assert [len(p) for p in pages] == [4, 4, 3]
    assert flat(await walk(client, "/agents", "agents", 200)) == expected


async def test_agents_carry_their_latest_version_summary_only(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    rows = await add_agents(engine, 3)
    by_age = newest_first(rows)  # [newest, ..., oldest]
    newest, middle, oldest = (uuid.UUID(i) for i in by_age)
    await add_version(engine, newest, 1, "m-1")
    await add_version(engine, newest, 2, "m-2")
    await add_version(engine, newest, 3, "m-3")
    await add_version(engine, middle, 1, "only")
    body = (await client.get(f"{API}/agents")).json()
    got = {a["id"]: a["latest_version"] for a in body["agents"]}
    assert got[str(newest)]["version"] == 3 and got[str(newest)]["model"] == "m-3"
    assert got[str(newest)]["tools"] == ["read_file", "run_tests"]
    assert got[str(newest)]["model_provider"] == "fake"
    assert got[str(middle)]["version"] == 1
    assert got[str(oldest)] is None
    assert "SECRET-PROMPT" not in str(body)
    # The per-agent versions listing is unchanged.
    versions = await client.get(f"{API}/agents/{newest}/versions")
    assert versions.status_code == 200 and len(versions.json()) == 3


# -- tasks -------------------------------------------------------------------------------------

TASK = """INSERT INTO tasks (id, name, task_type, image, command, instructions, files, timeout_s,
    memory_mb, cpu_limit, dataset_id, difficulty, evaluation, created_at)
    VALUES (:id, :name, :type, 'img', CAST(:cmd AS jsonb), :ins, '{}', 30, 256, 1.0, :ds, :diff,
      CAST(:ev AS jsonb), :at)"""


async def add_dataset(
    engine: AsyncEngine, name: str, at: datetime, *, frozen: bool = False
) -> uuid.UUID:
    ds = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO datasets (id, name, version, status, frozen_at, created_at) "
                "VALUES (:id, :n, '1', :s, :f, :at)"
            ),
            {
                "id": ds,
                "n": name,
                "s": "FROZEN" if frozen else "DRAFT",
                "f": at if frozen else None,
                "at": at,
            },
        )
    return ds


async def add_tasks(engine: AsyncEngine, dataset_id: uuid.UUID) -> dict[str, list[Any]]:
    """24 tasks: type, difficulty and dataset membership are all derived from the index."""
    rows: dict[str, list[Any]] = {"all": [], "agent": [], "hard": [], "ds": [], "agent_hard_ds": []}
    async with engine.begin() as conn:
        for i, at in enumerate(stamps(24, 5)):
            agent = i % 2 == 0
            diff = ("easy", "medium", "hard")[i % 3] if agent else None
            in_ds = agent and i % 4 == 0
            task_id = uuid.uuid4()
            await conn.execute(
                text(TASK),
                {
                    "id": task_id,
                    "name": f"t{i}",
                    "type": "agent" if agent else "command",
                    "cmd": None if agent else '["true"]',
                    "ins": "do it" if agent else None,
                    "ds": dataset_id if in_ds else None,
                    "diff": diff,
                    "ev": '{"hidden": "SPEC-SECRET"}' if agent and i % 3 == 0 else None,
                    "at": at,
                },
            )
            item = (at, task_id)
            rows["all"].append(item)
            if agent:
                rows["agent"].append(item)
            if diff == "hard":
                rows["hard"].append(item)
            if in_ds:
                rows["ds"].append(item)
            if in_ds and diff == "hard":
                rows["agent_hard_ds"].append(item)
    return rows


async def test_tasks_page_and_filters(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    ds = await add_dataset(engine, "d", T0)
    rows = await add_tasks(engine, ds)
    assert flat(await walk(client, "/tasks", "tasks", 5)) == newest_first(rows["all"])
    assert flat(await walk(client, "/tasks", "tasks", 4, task_type="agent")) == newest_first(
        rows["agent"]
    )
    assert flat(await walk(client, "/tasks", "tasks", 2, dataset_id=str(ds))) == newest_first(
        rows["ds"]
    )
    assert flat(await walk(client, "/tasks", "tasks", 3, difficulty="hard")) == newest_first(
        rows["hard"]
    )
    both = await walk(client, "/tasks", "tasks", 1, dataset_id=str(ds), difficulty="hard")
    assert flat(both) == newest_first(rows["agent_hard_ds"])
    nothing = await walk(client, "/tasks", "tasks", 5, dataset_id=str(uuid.uuid4()))
    assert nothing == [[]]


async def test_tasks_list_is_public_fields_only(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await add_tasks(engine, await add_dataset(engine, "d", T0))
    resp = await client.get(f"{API}/tasks", params={"limit": 200})
    assert "SPEC-SECRET" not in resp.text
    items = resp.json()["tasks"]
    assert {t["has_evaluation"] for t in items} == {True, False}
    for t in items:
        assert not {"evaluation", "files", "instructions", "command"} & set(t)


# -- datasets ----------------------------------------------------------------------------------


async def test_datasets_page_status_filter_and_task_counts(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    rows, frozen = [], []
    for i, at in enumerate(stamps(10)):
        is_frozen = i % 3 == 0
        ds = await add_dataset(engine, f"ds-{i}", at, frozen=is_frozen)
        rows.append((at, ds))
        if is_frozen:
            frozen.append((at, ds))
    expected_frozen = newest_first(frozen)
    assert flat(await walk(client, "/datasets", "datasets", 3)) == newest_first(rows)
    assert flat(await walk(client, "/datasets", "datasets", 2, status="FROZEN")) == expected_frozen
    drafts = [r for r in rows if r not in frozen]
    assert flat(await walk(client, "/datasets", "datasets", 4, status="DRAFT")) == newest_first(
        drafts
    )
    async with engine.begin() as conn:
        for i in range(3):
            await conn.execute(
                text(TASK),
                {
                    "id": uuid.uuid4(), "name": f"c{i}", "type": "agent", "cmd": None,
                    "ins": "x", "ds": rows[0][1], "diff": None, "ev": None, "at": T0,
                },
            )  # fmt: skip
    listed = (await client.get(f"{API}/datasets", params={"limit": 200})).json()["datasets"]
    counts = {d["id"]: d["task_count"] for d in listed}
    assert counts[str(rows[0][1])] == 3
    assert sum(counts.values()) == 3
    assert all("task_ids" not in d for d in listed)


# -- evaluations -------------------------------------------------------------------------------


async def test_evaluations_page_filters_and_progress_matches_detail(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    ds1, _ = await frozen_dataset(client)
    ds2, _ = await frozen_dataset(client)
    v1, v2 = await agent_version(sessionmaker), await agent_version(sessionmaker)
    first = [(await create_evaluation(client, ds1, v1, repetitions=1)).json() for _ in range(5)]
    second = [(await create_evaluation(client, ds2, v2, repetitions=2)).json() for _ in range(3)]
    # Same dataset, other agent version.
    mixed = (await create_evaluation(client, ds1, v2, repetitions=1)).json()
    everything = [*first, *second, mixed]
    created = {e["id"]: e["created_at"] for e in everything}
    order = sorted(created, key=lambda i: (created[i], i), reverse=True)

    assert flat(await walk(client, "/evaluations", "evaluations", 2)) == order
    only_v1 = [e["id"] for e in first]
    got = flat(await walk(client, "/evaluations", "evaluations", 2, agent_version_id=str(v1)))
    assert got == sorted(only_v1, key=lambda i: (created[i], i), reverse=True)
    ds1_ids = [e["id"] for e in [*first, mixed]]
    got = flat(await walk(client, "/evaluations", "evaluations", 3, dataset_id=ds1))
    assert got == sorted(ds1_ids, key=lambda i: (created[i], i), reverse=True)
    both = await walk(
        client, "/evaluations", "evaluations", 5, dataset_id=ds1, agent_version_id=str(v2)
    )
    assert flat(both) == [mixed["id"]]

    listing = (await client.get(f"{API}/evaluations", params={"limit": 200})).json()
    assert len(listing["evaluations"]) == len(everything)
    for item in listing["evaluations"]:
        detail = (await client.get(f"{API}/evaluations/{item['id']}")).json()
        assert item == detail
    assert listing["evaluations"][0]["progress"]["runs_total"] > 0


async def test_evaluations_list_progress_reflects_run_states(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)
    item = (await client.get(f"{API}/evaluations")).json()["evaluations"][0]
    assert item["id"] == s.evaluation_id
    progress = item["progress"]
    assert progress["runs_total"] == 9
    assert progress["runs_by_status"]["COMPLETED"] == 6
    assert progress["runs_by_status"]["FAILED"] == 2
    assert progress["runs_by_status"]["QUEUED"] == 1
    assert progress["complete"] is False


# -- workers -----------------------------------------------------------------------------------

WORKER = """INSERT INTO workers (id, hostname, pid, version, concurrency, started_at,
    last_heartbeat_at, stopped_at, stop_reason)
    VALUES (:id, 'host', 1, '0.1', 2, :started, :beat, :stopped, :reason)"""


async def add_workers(engine: AsyncEngine) -> list[str]:
    """Expected order: not-stopped by started_at desc then id desc, then stopped ones likewise."""
    now = datetime.now(UTC)
    spec: list[tuple[str, datetime, datetime, bool]] = [
        ("w-a", T0 + timedelta(minutes=1), now, False),
        ("w-b", T0 + timedelta(minutes=1), now - timedelta(seconds=60), False),  # tie with w-a
        ("w-c", T0 + timedelta(minutes=2), now - timedelta(minutes=10), False),  # silent
        ("w-d", T0 + timedelta(minutes=3), now, True),
        ("w-e", T0 + timedelta(minutes=9), now, True),
        ("w-f", T0, now, False),
        ("w-g", T0 + timedelta(minutes=9), now, True),
    ]
    async with engine.begin() as conn:
        for wid, started, beat, stopped in spec:
            await conn.execute(
                text(WORKER),
                {
                    "id": wid, "started": started, "beat": beat,
                    "stopped": now if stopped else None,
                    "reason": ("shutdown" if wid != "w-g" else "lost") if stopped else None,
                },
            )  # fmt: skip
    return ["w-c", "w-b", "w-a", "w-f", "w-g", "w-e", "w-d"]


async def test_workers_live_first_paged_with_alive_flag(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    expected = await add_workers(engine)
    for limit in (1, 2, 3, 7, 200):
        pages = await walk(client, "/workers", "workers", limit)
        assert flat(pages) == expected, limit
    body = (await client.get(f"{API}/workers")).json()
    workers = {w["id"]: w for w in body["workers"]}
    # alive: not stopped and a heartbeat within 3 x lease ttl (30 s default => 90 s).
    assert workers["w-a"]["alive"] is True
    assert workers["w-b"]["alive"] is True  # 60 s old
    assert workers["w-c"]["alive"] is False  # not stopped, but silent for 10 min
    assert workers["w-d"]["alive"] is False  # stopped with a fresh heartbeat
    assert workers["w-g"]["stop_reason"] == "lost" and workers["w-g"]["stopped_at"] is not None
    assert workers["w-a"]["stop_reason"] is None and workers["w-a"]["stopped_at"] is None
    assert set(workers["w-a"]) == {
        "id", "hostname", "version", "concurrency", "started_at", "last_heartbeat_at",
        "stopped_at", "stop_reason", "alive",
    }  # fmt: skip


async def test_workers_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{API}/workers")
    assert resp.json() == {"workers": [], "next_after": None}


# -- stats -------------------------------------------------------------------------------------


async def test_stats_on_an_empty_database_are_zeros_and_nulls(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{API}/stats/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window_h"] == 24
    assert body["evaluations"] == {"total": 0, "cancel_requested": 0}
    assert body["runs"]["total"] == 0
    assert set(body["runs"]["by_status"].values()) == {0}
    assert body["success"] == {"graded": 0, "passed": 0, "success_rate": None}
    assert body["run_latency_ms"] == {"count": 0, "p50": None, "p95": None}
    assert body["usage"] == {"attempts": 0, "cost_usd": "0", "tokens_in": 0, "tokens_out": 0}
    assert body["active_workers"] == 0
    assert body["queue"] == {
        "runs_queued": 0, "runs_retrying": 0, "evaluations_pending": 0, "total": 0,
    }  # fmt: skip


async def give_latencies(engine: AsyncEngine) -> None:
    """The seed leaves runs.started_at empty: set it so run N took its attempt's duration_ms
    (100..700 ms for A1 A2 A3 B1 B2 C1 C2; B3 has no attempt and never started)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runs SET started_at = runs.finished_at - a.duration_ms * interval '1 ms' "
                "FROM run_attempts a WHERE a.run_id = runs.id"
            )
        )


async def test_stats_definitions_against_seeded_data(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    await seed(client, sessionmaker)
    await give_latencies(engine)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(WORKER),
            {"id": "w-live", "started": now, "beat": now, "stopped": None, "reason": None},
        )
        await conn.execute(
            text(WORKER),
            {
                "id": "w-silent", "started": now, "beat": now - timedelta(minutes=5),
                "stopped": None, "reason": None,
            },
        )  # fmt: skip
        await conn.execute(
            text(WORKER),
            {"id": "w-stopped", "started": now, "beat": now, "stopped": now, "reason": "shutdown"},
        )
    body = (await client.get(f"{API}/stats/overview")).json()

    assert body["evaluations"] == {"total": 1, "cancel_requested": 0}
    assert body["runs"]["total"] == 9
    assert body["runs"]["by_status"] == {
        "QUEUED": 1, "CLAIMED": 0, "RUNNING": 0, "RETRYING": 0,
        "COMPLETED": 6, "FAILED": 2, "CANCELLED": 0,
    }  # fmt: skip
    # SUCCEEDED evaluations: A1 passed, A2 tests_failed, A3 tampering. B2 is ERROR, B1 and C1
    # are SKIPPED, C2 is RUNNING: none of those are graded.
    assert body["success"]["graded"] == 3 and body["success"]["passed"] == 1
    assert body["success"]["success_rate"] == pytest.approx(1 / 3)
    # Runs with started_at and finished_at: durations 100..700 ms, B3 never started.
    lat = body["run_latency_ms"]
    assert lat["count"] == 7
    assert lat["p50"] == pytest.approx(400, abs=1)
    assert lat["p95"] == pytest.approx(670, abs=1)  # 600 + 0.7 * (700 - 600)
    # 7 attempts, each 0.010000 USD, 100 in, 20 out.
    assert body["usage"] == {
        "attempts": 7, "cost_usd": "0.070000", "tokens_in": 700, "tokens_out": 140,
    }  # fmt: skip
    assert body["active_workers"] == 1
    assert body["queue"] == {
        "runs_queued": 1, "runs_retrying": 0, "evaluations_pending": 0, "total": 1,
    }  # fmt: skip


async def test_stats_window_restricts_success_latency_and_usage_but_not_counts(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    s = await seed(client, sessionmaker)
    await give_latencies(engine)
    old = datetime.now(UTC) - timedelta(hours=48)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runs SET finished_at = CAST(:old AS timestamptz), "
                "started_at = CAST(:old AS timestamptz) - interval '100 ms' WHERE id = :id"
            ),
            {"old": old, "id": s.runs["A1"]},
        )
        await conn.execute(
            text("UPDATE run_attempts SET finished_at = :old WHERE run_id = :id"),
            {"old": old, "id": s.runs["A1"]},
        )
    day = (await client.get(f"{API}/stats/overview")).json()
    assert day["runs"]["total"] == 9  # counts are all-time
    # A1 (the only pass) fell out of the window: 2 graded, 0 passed.
    assert day["success"] == {"graded": 2, "passed": 0, "success_rate": 0.0}
    assert day["run_latency_ms"]["count"] == 6
    assert day["run_latency_ms"]["p50"] == pytest.approx(450, abs=1)
    assert day["run_latency_ms"]["p95"] == pytest.approx(675, abs=1)
    assert day["usage"]["attempts"] == 6 and day["usage"]["cost_usd"] == "0.060000"
    assert day["usage"]["tokens_in"] == 600

    three_days = (await client.get(f"{API}/stats/overview", params={"window_h": 72})).json()
    assert three_days["window_h"] == 72
    assert three_days["success"]["graded"] == 3 and three_days["success"]["passed"] == 1
    assert three_days["usage"]["attempts"] == 7
    assert three_days["run_latency_ms"]["count"] == 7


async def test_stats_queue_depth_counts_retrying_runs_and_pending_evaluations(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    s = await seed(client, sessionmaker)
    async with engine.begin() as conn:
        # C3 is QUEUED. Make A3 RETRYING (needs next_attempt_at, no outcome) and B1's
        # evaluation PENDING (a PENDING row has no verdict, and a failure-reason is optional).
        await conn.execute(
            text(
                "UPDATE runs SET status = 'RETRYING', outcome = NULL, error_message = NULL, "
                "next_attempt_at = now() + interval '1 min' WHERE id = :id"
            ),
            {"id": s.runs["A3"]},
        )
        await conn.execute(
            text(
                """UPDATE attempt_evaluations SET status = 'PENDING', task_success = NULL,
                     failure_category = NULL, finished_at = NULL, started_at = NULL
                   WHERE attempt_id IN (SELECT id FROM run_attempts WHERE run_id = :id)"""
            ),
            {"id": s.runs["B1"]},
        )
    queue = (await client.get(f"{API}/stats/overview")).json()["queue"]
    assert queue == {
        "runs_queued": 1, "runs_retrying": 1, "evaluations_pending": 1, "total": 3,
    }  # fmt: skip


# -- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,params",
    [
        ("/agents", {"limit": 0}),
        ("/agents", {"limit": 201}),
        ("/agents", {"after": "garbage"}),
        ("/agents", {"after": "2026-01-01T00:00:00.000000Z|not-a-uuid"}),
        ("/agents", {"after": "2026-01-01T00:00:00.000000|" + str(uuid.uuid4())}),  # naive stamp
        ("/tasks", {"limit": 0}),
        ("/tasks", {"after": "garbage"}),
        ("/tasks", {"task_type": "bogus"}),
        ("/tasks", {"difficulty": "impossible"}),
        ("/tasks", {"dataset_id": "not-a-uuid"}),
        ("/datasets", {"status": "ARCHIVED"}),
        ("/datasets", {"limit": 500}),
        ("/datasets", {"after": "garbage"}),
        ("/evaluations", {"agent_version_id": "x"}),
        ("/evaluations", {"dataset_id": "x"}),
        ("/evaluations", {"limit": 0}),
        ("/evaluations", {"after": "garbage"}),
        ("/workers", {"limit": 0}),
        ("/workers", {"limit": 201}),
        ("/workers", {"after": "garbage"}),
        ("/workers", {"after": "2|2026-01-01T00:00:00.000000Z|w"}),
        ("/workers", {"after": "0|not-a-time|w"}),
        ("/stats/overview", {"window_h": 0}),
        ("/stats/overview", {"window_h": 721}),
        ("/stats/overview", {"window_h": "soon"}),
    ],
)
async def test_bad_parameters_are_422(
    client: httpx.AsyncClient, path: str, params: dict[str, Any]
) -> None:
    resp = await client.get(f"{API}{path}", params=params)
    assert resp.status_code == 422, resp.text


async def test_stats_window_boundaries_are_accepted(client: httpx.AsyncClient) -> None:
    for window in (1, 720):
        resp = await client.get(f"{API}/stats/overview", params={"window_h": window})
        assert resp.status_code == 200 and resp.json()["window_h"] == window


# -- CORS --------------------------------------------------------------------------------------


async def preflight(
    settings: Settings, origin: str, method: str = "POST", headers: str = "content-type"
) -> httpx.Response:
    app = create_app(settings)  # preflights are answered by the middleware, no lifespan needed
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.options(
            f"{API}/runs",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": headers,
            },
        )


async def test_cors_preflight_allows_the_default_origin_only(settings: Settings) -> None:
    ok = await preflight(settings, "http://localhost:3000", headers="content-type,idempotency-key")
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert {m.strip() for m in ok.headers["access-control-allow-methods"].split(",")} == {
        "GET", "POST",
    }  # fmt: skip
    assert "access-control-allow-credentials" not in ok.headers

    refused = await preflight(settings, "http://evil.example")
    assert refused.status_code == 400
    assert "access-control-allow-origin" not in refused.headers


async def test_cors_origins_are_configurable_and_methods_and_headers_restricted(
    settings: Settings,
) -> None:
    custom = settings.model_copy(update={"cors_origins": ["https://dash.example"]})
    assert (await preflight(custom, "https://dash.example")).status_code == 200
    assert (await preflight(custom, "http://localhost:3000")).status_code == 400
    assert (await preflight(custom, "https://dash.example", method="DELETE")).status_code == 400
    assert (await preflight(custom, "https://dash.example", headers="x-evil")).status_code == 400


async def test_cors_header_on_a_simple_request(
    client: httpx.AsyncClient,
) -> None:
    allowed = await client.get(f"{API}/workers", headers={"Origin": "http://localhost:3000"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    other = await client.get(f"{API}/workers", headers={"Origin": "http://evil.example"})
    assert other.status_code == 200 and "access-control-allow-origin" not in other.headers


def test_cors_origins_setting_parses_a_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().cors_origins == ["http://localhost:3000"]
    monkeypatch.setenv("AGENTEVAL_CORS_ORIGINS", '["http://a.example","https://b.example"]')
    assert Settings().cors_origins == ["http://a.example", "https://b.example"]
