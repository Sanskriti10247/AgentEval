"""Security review: hostile input at the API boundary (F-A1, F-A2). Real app, Postgres, Redis."""

import httpx

from tests.integration.conftest import SANDBOX_IMAGE, task_body


async def test_a_nul_character_is_a_422_not_a_500(client: httpx.AsyncClient) -> None:
    """Postgres cannot store U+0000 in text or jsonb; the schemas do not reject it, so the
    database error is mapped to a fixed-text 422 that does not echo the value."""
    resp = await client.post(
        "/api/v1/tasks", json=task_body(files={"main.py": "print('a\u0000b')"})
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "main.py" not in resp.text and "print" not in resp.text


async def test_the_api_keeps_working_after_a_rejected_value(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/tasks", json=task_body(files={"a.py": "\u0000"}))
    ok = await client.post("/api/v1/tasks", json=task_body())
    assert ok.status_code == 201


async def test_an_oversize_body_is_413_with_the_configured_limit(
    client: httpx.AsyncClient,
) -> None:
    limit = client.app.state.settings.max_request_bytes  # type: ignore[attr-defined]
    huge = "x" * (limit + 1)
    resp = await client.post(
        "/api/v1/tasks",
        content=f'{{"name": "n", "image": "{SANDBOX_IMAGE}", "command": ["true"], "files": '
        f'{{"a.py": "{huge}"}}}}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["title"] == "Payload Too Large"


async def test_the_413_is_counted_by_the_metrics(client: httpx.AsyncClient) -> None:
    limit = client.app.state.settings.max_request_bytes  # type: ignore[attr-defined]
    await client.post(
        "/api/v1/agents",
        content=b"x" * (limit + 1),
        headers={"content-type": "application/json"},
    )
    metrics = await client.get("/metrics")
    assert 'route="/api/v1/agents",status="413"' in metrics.text
