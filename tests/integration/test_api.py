import asyncio
import uuid

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from agenteval_core.config import Settings
from tests.integration.conftest import create_task, task_body


async def stream_run_ids(redis: Redis, settings: Settings) -> list[str]:
    entries = await redis.xrange(settings.dispatch_stream)
    return [fields[b"run_id"].decode() for _, fields in entries]


async def test_health_and_ready(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {
        "status": "ok",
        "read_only": False,
        "public_demo": False,
        "demo_limits": None,
    }
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"ready": True, "checks": {"postgres": "ok", "redis": "ok"}}


async def test_create_and_get_task(client: httpx.AsyncClient) -> None:
    task_id = await create_task(client)
    resp = await client.get(f"/api/v1/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["command"] == ["python", "main.py"]
    assert body["files"]["main.py"].startswith("print(")


async def test_non_allowlisted_image_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/tasks", json=task_body(image="ubuntu:latest"))
    assert resp.status_code == 422
    assert "allowlist" in resp.json()["detail"]


async def test_invalid_task_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/tasks", json=task_body(files={"../x": "y"}, timeout_s=0))
    assert resp.status_code == 422


async def test_unknown_ids_404(client: httpx.AsyncClient) -> None:
    missing = uuid.uuid4()
    assert (await client.get(f"/api/v1/tasks/{missing}")).status_code == 404
    assert (await client.get(f"/api/v1/runs/{missing}")).status_code == 404
    assert (await client.get(f"/api/v1/runs/{missing}/attempts")).status_code == 404
    resp = await client.post("/api/v1/runs", json={"task_id": str(missing)})
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"


async def test_submit_run_persists_queued_and_dispatches(
    client: httpx.AsyncClient, redis: Redis, settings: Settings
) -> None:
    task_id = await create_task(client)
    resp = await client.post("/api/v1/runs", json={"task_id": task_id})
    assert resp.status_code == 202
    run = resp.json()
    assert run["status"] == "QUEUED"
    assert run["outcome"] is None
    assert run["attempt_count"] == 0
    assert await stream_run_ids(redis, settings) == [run["id"]]

    fetched = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert fetched["status"] == "QUEUED"
    assert (await client.get(f"/api/v1/runs/{run['id']}/attempts")).json() == []


async def test_same_idempotency_key_returns_same_run(client: httpx.AsyncClient) -> None:
    task_id = await create_task(client)
    headers = {"Idempotency-Key": "abc-1"}
    first = await client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers)
    second = await client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers)
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["id"] == second.json()["id"]


async def test_concurrent_duplicate_submits_create_one_run(client: httpx.AsyncClient) -> None:
    task_id = await create_task(client)
    headers = {"Idempotency-Key": "race-1"}
    responses = await asyncio.gather(
        *[client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers) for _ in range(8)]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1


async def test_replay_of_queued_run_redispatches(
    client: httpx.AsyncClient, redis: Redis, settings: Settings
) -> None:
    task_id = await create_task(client)
    headers = {"Idempotency-Key": "replay-1"}
    run_id = (await client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers)).json()[
        "id"
    ]
    await client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers)
    # Still QUEUED -> dispatched again (duplicates are safe: conditional claim).
    assert await stream_run_ids(redis, settings) == [run_id, run_id]


async def test_idempotency_key_reused_for_other_task_conflicts(client: httpx.AsyncClient) -> None:
    t1, t2 = await create_task(client), await create_task(client)
    headers = {"Idempotency-Key": "shared"}
    await client.post("/api/v1/runs", json={"task_id": t1}, headers=headers)
    resp = await client.post("/api/v1/runs", json={"task_id": t2}, headers=headers)
    assert resp.status_code == 409


async def test_dispatch_failure_returns_503_and_retry_recovers(
    client: httpx.AsyncClient, redis: Redis, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await create_task(client)
    queue = client.app.state.queue  # type: ignore[attr-defined]
    real_publish = queue.publish

    async def broken_publish(run_id: uuid.UUID) -> str:
        raise RedisConnectionError("redis down")

    monkeypatch.setattr(queue, "publish", broken_publish)
    headers = {"Idempotency-Key": "flaky-1"}
    resp = await client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert await stream_run_ids(redis, settings) == []

    # The run is durable in Postgres even though dispatch failed.
    monkeypatch.setattr(queue, "publish", real_publish)
    retry = await client.post("/api/v1/runs", json={"task_id": task_id}, headers=headers)
    assert retry.status_code == 200
    assert retry.json()["status"] == "QUEUED"
    assert await stream_run_ids(redis, settings) == [retry.json()["id"]]
