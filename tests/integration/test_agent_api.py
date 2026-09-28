"""Agent API: agents, immutable versions, agent tasks, runs and trajectory
paging, over HTTP against real Postgres/Redis."""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import docker
import httpx
import pytest

from agenteval_api.main import create_app
from agenteval_core.agent.events import EventType, TrajectoryEventData
from agenteval_core.config import Settings
from agenteval_core.providers.base import Usage
from agenteval_core.providers.fake import FakeProvider
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.runs import RunRepository
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.executor import Executor
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox
from agenteval_worker.trajectory import TrajectoryWriter
from tests.integration.conftest import SANDBOX_IMAGE

API = "/api/v1"
SCRIPT = [{"type": "turn", "text": "done"}]


def version_body(**overrides: Any) -> dict[str, Any]:
    return {
        "model_provider": "fake",
        "model": "fake-model",
        "system_prompt": "You fix bugs.",
        "tools": ["read_file", "write_file", "run_tests"],
        "model_settings": {"script": SCRIPT},
        **overrides,
    }


def agent_task_body(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "fix-add",
        "task_type": "agent",
        "image": SANDBOX_IMAGE,
        "instructions": "Fix calc.py so the tests pass.",
        "test_command": ["python", "-m", "unittest", "-q"],
        "files": {"calc.py": "def add(a, b):\n    return a - b\n"},
        "timeout_s": 600,
        **overrides,
    }


async def new_agent(client: httpx.AsyncClient, name: str | None = None) -> str:
    resp = await client.post(f"{API}/agents", json={"name": name or f"a-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def new_version(client: httpx.AsyncClient, agent_id: str, **overrides: Any) -> dict[str, Any]:
    resp = await client.post(f"{API}/agents/{agent_id}/versions", json=version_body(**overrides))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# -- agents ---------------------------------------------------------------------------------------


async def test_create_get_and_unique_names(client: httpx.AsyncClient) -> None:
    agent_id = await new_agent(client, "fixer")
    assert (await client.get(f"{API}/agents/{agent_id}")).json()["name"] == "fixer"
    dup = await client.post(f"{API}/agents", json={"name": "fixer"})
    assert dup.status_code == 409 and dup.json()["title"] == "Conflict"
    assert (await client.post(f"{API}/agents", json={"name": "has space"})).status_code == 422
    assert (await client.get(f"{API}/agents/{uuid.uuid4()}")).status_code == 404


# -- versions -------------------------------------------------------------------------------------


async def test_versions_are_immutable_and_idempotent(client: httpx.AsyncClient) -> None:
    agent_id = await new_agent(client)
    v1 = await client.post(f"{API}/agents/{agent_id}/versions", json=version_body())
    assert v1.status_code == 201 and v1.json()["version"] == 1
    body = v1.json()
    assert body["tools"] == ["read_file", "run_tests", "write_file"]  # normalised order
    assert body["limits"]["max_steps"] == 30  # defaults filled in
    assert len(body["config_hash"]) == 64

    again = await client.post(f"{API}/agents/{agent_id}/versions", json=version_body())
    assert again.status_code == 200 and again.json()["id"] == body["id"]

    # Equivalent config: other tool order, a default made explicit.
    same = await client.post(
        f"{API}/agents/{agent_id}/versions",
        json=version_body(
            tools=["write_file", "run_tests", "read_file", "read_file"], limits={"max_steps": 30}
        ),
    )
    assert same.status_code == 200 and same.json()["config_hash"] == body["config_hash"]

    v2 = await new_version(client, agent_id, system_prompt="Be terse.")
    assert v2["version"] == 2 and v2["config_hash"] != body["config_hash"]
    listed = (await client.get(f"{API}/agents/{agent_id}/versions")).json()
    assert [v["version"] for v in listed] == [1, 2]
    got = await client.get(f"{API}/agents/{agent_id}/versions/2")
    assert got.json()["system_prompt"] == "Be terse."
    assert (await client.get(f"{API}/agents/{agent_id}/versions/3")).status_code == 404


async def test_same_config_under_different_agents_is_two_versions(
    client: httpx.AsyncClient,
) -> None:
    a, b = await new_agent(client), await new_agent(client)
    va, vb = await new_version(client, a), await new_version(client, b)
    assert va["config_hash"] == vb["config_hash"] and va["id"] != vb["id"]


async def test_concurrent_identical_creates_make_one_version(client: httpx.AsyncClient) -> None:
    agent_id = await new_agent(client)
    responses = await asyncio.gather(
        *[client.post(f"{API}/agents/{agent_id}/versions", json=version_body()) for _ in range(8)]
    )
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["id"] for r in responses}) == 1


async def test_concurrent_different_creates_get_distinct_numbers(client: httpx.AsyncClient) -> None:
    agent_id = await new_agent(client)
    responses = await asyncio.gather(
        *[
            client.post(
                f"{API}/agents/{agent_id}/versions", json=version_body(system_prompt=f"p{i}")
            )
            for i in range(6)
        ]
    )
    assert all(r.status_code == 201 for r in responses)
    assert sorted(r.json()["version"] for r in responses) == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"tools": ["read_file", "browse"]}, "unknown tools: browse"),
        ({"tools": ["shell"]}, "needs a non-empty shell_allowlist"),
        ({"shell_allowlist": ["python"]}, "shell tool is not granted"),
        (
            {"model_provider": "anthropic", "model": "claude-opus-5", "model_settings": {}},
            "'anthropic' is not enabled",
        ),
        ({"model": "gpt-9"}, "no price for fake/gpt-9"),
        ({"limits": {"max_steps": 0}}, "invalid limits"),
        ({"limits": {"surprise": 1}}, "invalid limits"),
        ({"limits": {"max_steps": 150}}, "max_steps 150 > cap 100"),
        ({"limits": {"max_cost_usd": "9.50"}}, "max_cost_usd 9.50 > cap 5.00"),
        ({"model_settings": {"script": []}}, "invalid fake script"),
        (
            {"model_settings": {"script": [{"type": "turn", "stop": "tool_use"}]}},
            "invalid fake script",
        ),
    ],
)
async def test_invalid_versions_are_rejected(
    client: httpx.AsyncClient, overrides: dict[str, Any], fragment: str
) -> None:
    agent_id = await new_agent(client)
    resp = await client.post(f"{API}/agents/{agent_id}/versions", json=version_body(**overrides))
    assert resp.status_code == 422, resp.text
    assert fragment in resp.json()["detail"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"shell_allowlist": ["/bin/sh"]},  # paths are never allowed
        {"shell_allowlist": ["rm -rf"]},
        {"tools": []},
        {"model_provider": "openai"},
        {"extra": True},
    ],
)
async def test_malformed_versions_fail_schema_validation(
    client: httpx.AsyncClient, overrides: dict[str, Any]
) -> None:
    agent_id = await new_agent(client)
    resp = await client.post(f"{API}/agents/{agent_id}/versions", json=version_body(**overrides))
    assert resp.status_code == 422


async def test_version_for_unknown_agent_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(f"{API}/agents/{uuid.uuid4()}/versions", json=version_body())
    assert resp.status_code == 404


async def test_fake_provider_can_be_disabled(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"enable_fake_provider": False}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            agent_id = await new_agent(c)
            resp = await c.post(f"{API}/agents/{agent_id}/versions", json=version_body())
            assert resp.status_code == 422 and "'fake' is not enabled" in resp.json()["detail"]


# -- agent tasks ----------------------------------------------------------------------------------


async def test_agent_task_round_trip(client: httpx.AsyncClient) -> None:
    resp = await client.post(f"{API}/tasks", json=agent_task_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["task_type"], body["command"], body["test_command"]) == (
        "agent",
        None,
        ["python", "-m", "unittest", "-q"],
    )
    assert (await client.get(f"{API}/tasks/{body['id']}")).json()["instructions"].startswith("Fix")


@pytest.mark.parametrize(
    "body",
    [
        agent_task_body(instructions=None),
        agent_task_body(command=["python", "x.py"]),
        agent_task_body(timeout_s=1801),
        agent_task_body(test_command=[]),
        agent_task_body(image="ubuntu:latest"),
        {"name": "c", "image": SANDBOX_IMAGE, "command": ["true"], "instructions": "no"},
        {"name": "c", "image": SANDBOX_IMAGE, "command": ["true"], "timeout_s": 301},
    ],
)
async def test_invalid_tasks_are_rejected(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    assert (await client.post(f"{API}/tasks", json=body)).status_code == 422


# -- runs -----------------------------------------------------------------------------------------


async def agent_run_fixture(client: httpx.AsyncClient) -> tuple[str, str]:
    agent_id = await new_agent(client)
    version = await new_version(client, agent_id)
    task_id = (await client.post(f"{API}/tasks", json=agent_task_body())).json()["id"]
    return task_id, version["id"]


async def test_agent_runs_pairing_rules(client: httpx.AsyncClient) -> None:
    task_id, version_id = await agent_run_fixture(client)
    cmd_task = (
        await client.post(
            f"{API}/tasks", json={"name": "c", "image": SANDBOX_IMAGE, "command": ["true"]}
        )
    ).json()["id"]

    missing = await client.post(f"{API}/runs", json={"task_id": task_id})
    assert missing.status_code == 422 and "need an agent_version_id" in missing.json()["detail"]
    extra = await client.post(
        f"{API}/runs", json={"task_id": cmd_task, "agent_version_id": version_id}
    )
    assert extra.status_code == 422
    unknown = await client.post(
        f"{API}/runs", json={"task_id": task_id, "agent_version_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404

    ok = await client.post(f"{API}/runs", json={"task_id": task_id, "agent_version_id": version_id})
    assert ok.status_code == 202 and ok.json()["agent_version_id"] == version_id
    assert (await client.post(f"{API}/runs", json={"task_id": cmd_task})).json()[
        "agent_version_id"
    ] is None  # S1 command runs unchanged


async def test_idempotency_covers_the_agent_version(client: httpx.AsyncClient) -> None:
    task_id, v1 = await agent_run_fixture(client)
    headers = {"Idempotency-Key": "agent-run-1"}
    first = await client.post(
        f"{API}/runs", json={"task_id": task_id, "agent_version_id": v1}, headers=headers
    )
    replay = await client.post(
        f"{API}/runs", json={"task_id": task_id, "agent_version_id": v1}, headers=headers
    )
    assert (first.status_code, replay.status_code) == (202, 200)
    assert first.json()["id"] == replay.json()["id"]
    other_agent = await new_agent(client)
    v_other = (await new_version(client, other_agent))["id"]
    clash = await client.post(
        f"{API}/runs", json={"task_id": task_id, "agent_version_id": v_other}, headers=headers
    )
    assert clash.status_code == 409


# -- trajectory paging ----------------------------------------------------------------------------


async def test_events_are_paged_by_seq(client: httpx.AsyncClient, sessionmaker: Any) -> None:
    task_id, version_id = await agent_run_fixture(client)
    run_id = (
        await client.post(f"{API}/runs", json={"task_id": task_id, "agent_version_id": version_id})
    ).json()["id"]
    async with sessionmaker() as s:
        repo = RunRepository(s)
        await repo.claim(uuid.UUID(run_id), "w1")
        attempt = await repo.start_attempt(uuid.UUID(run_id), "w1")
        await s.commit()
    assert attempt is not None
    events = [TrajectoryEventData(seq=0, step=0, event_type=EventType.RUN_STARTED)] + [
        TrajectoryEventData(
            seq=n,
            step=n,
            event_type=EventType.MODEL_CALL,
            usage=Usage(100, 20),
            cost_usd=Decimal("0.000200"),
            duration_ms=5,
        )
        for n in range(1, 5)
    ]
    await TrajectoryWriter(sessionmaker, attempt).append(events)

    base = f"{API}/runs/{run_id}/attempts/1/events"
    p1 = (await client.get(base, params={"limit": 2})).json()
    assert [e["seq"] for e in p1["events"]] == [0, 1] and p1["next_after_seq"] == 1
    p2 = (await client.get(base, params={"limit": 2, "after_seq": 1})).json()
    assert [e["seq"] for e in p2["events"]] == [2, 3] and p2["next_after_seq"] == 3
    p3 = (await client.get(base, params={"limit": 2, "after_seq": 3})).json()
    assert [e["seq"] for e in p3["events"]] == [4] and p3["next_after_seq"] is None
    assert p2["events"][0]["cost_usd"] == "0.000200" and p2["events"][0]["tokens_in"] == 100

    assert (await client.get(f"{API}/runs/{run_id}/attempts/2/events")).status_code == 404
    assert (await client.get(base, params={"limit": 0})).status_code == 422
    assert (await client.get(f"{API}/runs/{uuid.uuid4()}/attempts/1/events")).status_code == 404


# -- the whole flow over HTTP ---------------------------------------------------------------------


@pytest.mark.docker
async def test_create_run_and_inspect_an_agent_over_http(
    client: httpx.AsyncClient, sessionmaker: Any, queue: RunQueue
) -> None:
    agent_id = await new_agent(client, "http-fixer")
    fixed = "def add(a, b):\n    return a + b\n"
    version = await new_version(
        client,
        agent_id,
        model_settings={
            "script": [
                {
                    "type": "turn",
                    "tool_calls": [
                        {"name": "write_file", "input": {"path": "calc.py", "content": fixed}}
                    ],
                },
                {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
                {
                    "type": "turn",
                    "text": "Fixed.",
                    "expect_tool_results_contain": ["Tests passed."],
                },
            ]
        },
    )
    tests = (
        "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n"
        "    def test(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    task = (
        await client.post(
            f"{API}/tasks",
            json=agent_task_body(
                files={"calc.py": "def add(a, b):\n    return a - b\n", "test_calc.py": tests}
            ),
        )
    ).json()
    run = (
        await client.post(
            f"{API}/runs", json={"task_id": task["id"], "agent_version_id": version["id"]}
        )
    ).json()

    client_ = docker.from_env()
    runner = AgentRunner(
        sessionmaker=sessionmaker, docker_client=client_, providers={"fake": FakeProvider()}
    )
    ex = Executor(sessionmaker, queue, DockerSandbox(client_).run, "w-http", runner)
    [msg] = await queue.read("test", count=1, block_ms=1000)
    await ex.handle(msg)

    done = (await client.get(f"{API}/runs/{run['id']}")).json()
    assert (done["status"], done["outcome"]) == ("COMPLETED", "AGENT_FINISHED")
    [attempt] = (await client.get(f"{API}/runs/{run['id']}/attempts")).json()
    assert (attempt["steps"], attempt["final_answer"], attempt["cost_usd"]) == (
        3,
        "Fixed.",
        "0.000600",
    )
    events = (await client.get(f"{API}/runs/{run['id']}/attempts/1/events")).json()
    assert [e["event_type"] for e in events["events"]][-1] == "run_finished"
    assert [e["tool"] for e in events["events"] if e["tool"]] == ["write_file", "run_tests"]


async def test_anthropic_versions_when_enabled(settings: Settings) -> None:
    """The API validates Anthropic configs without the SDK (and without a key)."""
    app = create_app(settings.model_copy(update={"enable_anthropic_provider": True}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            agent_id = await new_agent(c)
            base = {"model_provider": "anthropic", "tools": ["read_file"]}

            opus = await c.post(
                f"{API}/agents/{agent_id}/versions", json={**base, "model": "claude-opus-5"}
            )
            assert opus.status_code == 201, opus.text
            assert opus.json()["model_settings"] == {"effort": "high"}  # explicit default

            same = await c.post(
                f"{API}/agents/{agent_id}/versions",
                json={**base, "model": "claude-opus-5", "model_settings": {"effort": "high"}},
            )
            assert same.status_code == 200 and same.json()["id"] == opus.json()["id"]

            haiku = await c.post(
                f"{API}/agents/{agent_id}/versions", json={**base, "model": "claude-haiku-4-5"}
            )
            assert haiku.status_code == 201 and haiku.json()["model_settings"] == {"effort": None}

            for bad, fragment in [
                (
                    {"model": "claude-haiku-4-5", "model_settings": {"effort": "high"}},
                    "does not support the effort",
                ),
                (
                    {"model": "claude-opus-5", "model_settings": {"fallbacks": "default"}},
                    "invalid Anthropic settings",
                ),
                ({"model": "claude-opus-5-20260401"}, "no price"),
            ]:
                resp = await c.post(f"{API}/agents/{agent_id}/versions", json={**base, **bad})
                assert resp.status_code == 422 and fragment in resp.json()["detail"], resp.text
