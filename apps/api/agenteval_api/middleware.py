"""HTTP request metrics (ADR-010 A) as a pure ASGI middleware.

Pure ASGI rather than `BaseHTTPMiddleware`: that class runs the app in a
separate task, which breaks contextvars (the log context) and buffers streaming
responses, and the dashboard's SSE endpoints (spec §3) are coming.

The route label is the route's full TEMPLATE (`/api/v1/runs/{run_id}`). The
router's own `scope["route"].path` is only the leaf path without the include
prefix (and its shape is FastAPI-version specific), so the templates are taken
from the public OpenAPI schema, which lists every route with its prefix in
declaration order, and the request path is matched against them with
Starlette's own path compiler. A request that matches no template (404, a
scanner probing random paths) is labelled `unmatched`, so the label set is
bounded by the number of routes, never by what clients send.
`/metrics` is not recorded: scraping must not move the numbers it reports.
"""

import re
import time

from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agenteval_core.observability import metrics

UNMATCHED = "unmatched"
EXCLUDED_PATHS = frozenset({"/metrics"})
# Served by FastAPI but absent from the OpenAPI schema.
EXTRA_TEMPLATES = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")

Templates = list[tuple[re.Pattern[str], str]]


def _compile(templates: list[str]) -> Templates:
    return [(compile_path(t)[0], t) for t in templates]


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._templates: Templates | None = None  # built on the first request

    def _route_label(self, scope: Scope) -> str:
        try:
            if self._templates is None:
                paths = list(scope["app"].openapi().get("paths", {}))
                self._templates = _compile([*paths, *EXTRA_TEMPLATES])
            path = scope.get("path", "")
            for pattern, template in self._templates:
                if pattern.match(path):
                    return template
        except Exception:  # labelling must never fail a request
            if self._templates is None:
                self._templates = []  # don't retry an expensive failure on every request
        return UNMATCHED

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500  # what a client sees if the app raises before responding

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.http_request(
                scope.get("method", ""),
                self._route_label(scope),
                status,
                time.perf_counter() - started,
            )
