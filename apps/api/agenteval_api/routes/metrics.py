"""GET /metrics: Prometheus text exposition.

Refreshes the scrape-time gauges from Postgres and Redis first (see
observability/scrape.py for why that is an async pre-scrape step rather than a
collector), then renders the registry. Overlapping scrapes are serialised: the
refresh clears and rewrites labelled gauges, and a second scrape must not
render them half way. This route is excluded from its own request metrics
(middleware.py) and from the OpenAPI schema.
"""

import asyncio

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agenteval_core.observability import scrape

router = APIRouter(tags=["observability"])

_lock = asyncio.Lock()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    settings = request.app.state.settings
    async with _lock:
        await scrape.refresh(
            request.app.state.sessionmaker,
            request.app.state.redis,
            streams=[settings.dispatch_stream, settings.evaluation_stream],
            group=settings.consumer_group,
            # The reconciler marks a worker lost after 3 x lease ttl of silence.
            live_worker_window_s=3 * settings.lease_ttl_s,
        )
        body = generate_latest()
    return Response(body, media_type=CONTENT_TYPE_LATEST)
