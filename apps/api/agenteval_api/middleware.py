"""HTTP request metrics (ADR-010 A) and the server span (ADR-010 B) as a pure ASGI middleware.

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

The server span is named `<METHOD> <route template>` and is the parent of every
span the request causes (`queue.publish`, ...). It carries the method, the
template and the status code, never the raw path, query or any header or body.
A W3C `traceparent` header on the request is honoured, so a caller's trace
continues here. `/metrics`, `/health` and `/ready` are not traced (probes and
scrapes would drown the real requests).
"""

import json
import re
import time

from opentelemetry.trace import SpanKind
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agenteval_core.observability import metrics, tracing

UNMATCHED = "unmatched"
EXCLUDED_PATHS = frozenset({"/metrics"})
UNTRACED_PATHS = frozenset({"/metrics", "/health", "/ready"})
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
        method = scope.get("method", "")
        route = self._route_label(scope)

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            if scope.get("path") in UNTRACED_PATHS:
                await self.app(scope, receive, send_wrapper)
            else:
                traceparent = _header(scope, tracing.TRACEPARENT_FIELD)
                # `remote` only when the caller sent one: no header is a plain root.
                with tracing.span(
                    f"{method} {route}",
                    kind=SpanKind.SERVER,
                    remote=traceparent is not None,
                    traceparent=traceparent,
                    attributes={"http.request.method": method, "http.route": route},
                ) as sp:
                    try:
                        await self.app(scope, receive, send_wrapper)
                    finally:
                        sp.set(**{"http.response.status_code": status})
                        if status >= 500:
                            sp.mark_error("http_5xx")
        finally:
            metrics.http_request(method, route, status, time.perf_counter() - started)


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.encode()
    for key, value in scope.get("headers", []):
        if key == wanted:
            return str(value.decode("latin-1"))
    return None


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Refuse a request body over `max_bytes` with 413 BEFORE it is parsed (security review
    F-A1). Without it uvicorn hands the whole body to the JSON parser, so one large POST
    costs memory proportional to its size; the per-field limits (task files, specs) only run
    after parsing. A declared `Content-Length` over the limit is refused without reading
    anything; a chunked or lying body is cut off as soon as the count passes the limit."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, "content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            await self._refuse(send)
            return

        seen = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if started:  # the response is already on the wire; all that is left is to stop
                raise
            await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        body = json.dumps(
            {
                "type": "about:blank",
                "title": "Payload Too Large",
                "status": 413,
                "detail": f"Request body exceeds {self._max} bytes",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
