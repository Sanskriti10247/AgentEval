"""GET /metrics and the HTTP middleware (ADR-010 A). No containers: Postgres and
Redis point at a closed port, which also proves the scrape never 500s when its
sources are down (the real-source values are tested in tests/integration)."""

import re
from collections.abc import AsyncIterator

import httpx
import pytest
from prometheus_client import REGISTRY

from agenteval_api.main import create_app
from agenteval_core.config import Settings

DEAD = Settings(
    database_url="postgresql+asyncpg://x:x@127.0.0.1:1/x",
    redis_url="redis://127.0.0.1:1/0",
    log_json=False,
    auth_enabled=False,  # no database to look keys up in; these tests are about /metrics
)
RUN_ID = "0b6c8f6a-79c1-4a2e-9d0f-3f0a4a1b2c3d"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(DEAD)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_metrics_is_text_exposition_with_the_expected_families(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    for family in (
        "agenteval_http_requests_total",
        "agenteval_http_request_duration_seconds",
        "agenteval_scrape_errors_total",
        "agenteval_runs_by_status",
        "agenteval_backlog_oldest_age_seconds",
        "agenteval_workers_live",
        "agenteval_stream_length",
        "agenteval_runs_finished_total",
        "agenteval_model_tokens_total",
    ):
        assert f"# TYPE {family} " in resp.text, family


async def test_requests_are_labelled_by_route_template_not_raw_path(
    client: httpx.AsyncClient,
) -> None:
    template = "/api/v1/runs/{run_id}"
    before = sample("agenteval_http_requests_total", method="GET", route=template, status="422")
    # Matches the route, fails path validation before any database access.
    assert (await client.get("/api/v1/runs/not-a-uuid")).status_code == 422
    assert (await client.get(f"/api/v1/runs/{RUN_ID[:-1]}x")).status_code == 422
    assert (
        sample("agenteval_http_requests_total", method="GET", route=template, status="422")
        == before + 2
    )

    health = sample("agenteval_http_requests_total", method="GET", route="/health", status="200")
    await client.get("/health")
    assert (
        sample("agenteval_http_requests_total", method="GET", route="/health", status="200")
        == health + 1
    )

    body = (await client.get("/metrics")).text
    assert 'route="/api/v1/runs/{run_id}"' in body
    assert "not-a-uuid" not in body and RUN_ID not in body


async def test_unmatched_paths_share_one_label(client: httpx.AsyncClient) -> None:
    before = sample("agenteval_http_requests_total", method="GET", route="unmatched", status="404")
    for path in ("/nope", f"/random/{RUN_ID}", "/wp-admin.php"):
        assert (await client.get(path)).status_code == 404
    assert (
        sample("agenteval_http_requests_total", method="GET", route="unmatched", status="404")
        == before + 3
    )


async def test_metrics_is_excluded_from_its_own_request_metrics(
    client: httpx.AsyncClient,
) -> None:
    for _ in range(3):
        await client.get("/metrics")
    body = (await client.get("/metrics")).text
    assert not re.search(r'agenteval_http_requests_total\{[^}]*route="/metrics"', body)


async def test_a_scrape_with_both_sources_down_is_200_and_counts_the_errors(
    client: httpx.AsyncClient,
) -> None:
    pg = sample("agenteval_scrape_errors_total", source="postgres")
    rd = sample("agenteval_scrape_errors_total", source="redis")
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert sample("agenteval_scrape_errors_total", source="postgres") == pg + 1
    assert sample("agenteval_scrape_errors_total", source="redis") == rd + 1
    # The failed sources' gauges are absent, not stale.
    assert "agenteval_runs_by_status{" not in resp.text
    assert "agenteval_stream_length{" not in resp.text
