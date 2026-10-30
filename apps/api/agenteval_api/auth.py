"""Authentication, authorization and rate limiting as ONE dependency per route
(ADR-012 A-C).

Every `/api/v1` route declares the scope it needs with `dependencies=[READ]`,
`[WRITE]` or `[ADMIN]`; a test sweeps the OpenAPI routes and fails if one does not.
The guard does, in order:

1. authenticate: `Authorization: Bearer <key>` -> 401 (+ `WWW-Authenticate: Bearer`)
   when missing, malformed, unknown, wrong, revoked or expired;
2. rate limit: the key's read budget for GET/HEAD/OPTIONS, else its write budget
   -> 429 with `Retry-After` and `X-RateLimit-*` (the headers also ride on successes);
3. authorize: the key must hold the route's scope -> 403.

The limit comes before the scope check on purpose: a caller hammering routes it may
not use is throttled like any other. With `AGENTEVAL_AUTH_ENABLED=false` the caller is
anonymous with every scope and the budgets are keyed by client IP (the address the
socket shows; behind a reverse proxy that is the proxy, so use the proxy's own limits
or leave auth on).

With `AGENTEVAL_READ_ONLY=true` (the read-only showcase) any route that needs more than
the read scope answers 403 before anything else, an `Authorization` header is ignored, and
every caller is an anonymous visitor holding only the read scope, rate limited by IP.

With `AGENTEVAL_PUBLIC_DEMO=true` the caller is the same anonymous visitor, but the routes
declared with `DEMO_WRITE` (launch an evaluation or a run, cancel, replay) are open to it
as well; every other write, and every admin route, answers 403 first. What those routes
may launch is capped in demo.py. READ_ONLY wins when both are set.
"""

from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agenteval_api.errors import ApiError
from agenteval_core.auth import ALL_SCOPES, AuthFailure, Scope
from agenteval_core.logging import get_logger
from agenteval_core.observability import metrics
from agenteval_core.services.api_keys import ApiKeyService

log = get_logger(__name__)

# auto_error=False: a missing header must be OUR 401 (problem+json), not FastAPI's 403.
_bearer = HTTPBearer(auto_error=False, description="An AgentEval API key (ae_...)")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_UNAUTHORIZED = {
    "missing": "Missing API key: send 'Authorization: Bearer <key>'",
    "invalid": "Invalid API key",
    "revoked": "This API key has been revoked",
    "expired": "This API key has expired",
}


@dataclass(frozen=True)
class Principal:
    name: str
    prefix: str | None  # None when authentication is disabled
    scopes: frozenset[Scope]


ANONYMOUS = Principal("anonymous", None, ALL_SCOPES)
VISITOR = Principal("visitor", None, frozenset({Scope.READ}))
DEMO_VISITOR = Principal("demo-visitor", None, frozenset({Scope.READ, Scope.WRITE}))
READ_ONLY_DETAIL = "This AgentEval deployment is a read-only demo; it accepts no changes"
PUBLIC_DEMO_DETAIL = (
    "This is a public demo: you can launch evaluations and runs of the existing agents and "
    "tasks, cancel them and replay failed runs. Nothing else can be changed here"
)


def _unauthorized(reason: str, request: Request) -> ApiError:
    metrics.auth_failure(reason)
    log.warning("auth_rejected", reason=reason, method=request.method, path=request.url.path)
    return ApiError(
        401, "Unauthorized", _UNAUTHORIZED[reason], headers={"WWW-Authenticate": "Bearer"}
    )


class ScopeGuard:
    """Callable FastAPI dependency; `.scope` is what the route-sweep test reads."""

    def __init__(self, scope: Scope, *, public_demo: bool = False) -> None:
        self.scope = scope
        # Open to the anonymous visitor of a public demo (only ever with Scope.WRITE).
        self.public_demo = public_demo

    async def __call__(
        self,
        request: Request,
        response: Response,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Principal:
        state = request.app.state
        settings = state.settings
        structlog.contextvars.unbind_contextvars("api_key_prefix", "api_key_name")

        client_ip = f"ip:{request.client.host if request.client else 'unknown'}"
        if settings.read_only:
            if self.scope is not Scope.READ:
                metrics.auth_failure("forbidden")
                raise ApiError(403, "Forbidden", READ_ONLY_DETAIL)
            principal, subject = VISITOR, client_ip
        elif settings.public_demo:
            if self.scope is not Scope.READ and not self.public_demo:
                metrics.auth_failure("forbidden")
                raise ApiError(403, "Forbidden", PUBLIC_DEMO_DETAIL)
            principal, subject = DEMO_VISITOR, client_ip
        elif settings.auth_enabled:
            principal, subject = await self._authenticate(request, credentials)
        else:
            principal, subject = ANONYMOUS, client_ip
        request.state.principal = principal

        kind = "read" if request.method in _SAFE_METHODS else "write"
        limit = (
            settings.rate_limit_read_per_min
            if kind == "read"
            else settings.rate_limit_write_per_min
        )
        decision = await state.ratelimiter.check(kind, subject, limit)
        if decision is not None:
            response.headers.update(decision.headers())
            if not decision.allowed:
                metrics.rate_limited(kind)
                log.warning("rate_limited", kind=kind, limit=limit, method=request.method)
                raise ApiError(
                    429,
                    "Too Many Requests",
                    f"Rate limit of {limit} {kind} requests per minute exceeded",
                    headers={**decision.headers(), "Retry-After": str(decision.reset_s)},
                )

        if self.scope not in principal.scopes:
            metrics.auth_failure("forbidden")
            log.warning(
                "auth_forbidden", scope=self.scope.value, method=request.method,
                path=request.url.path,
            )  # fmt: skip
            raise ApiError(403, "Forbidden", f"This API key lacks the {self.scope.value} scope")
        return principal

    @staticmethod
    async def _authenticate(
        request: Request, credentials: HTTPAuthorizationCredentials | None
    ) -> tuple[Principal, str]:
        token = credentials.credentials.strip() if credentials else ""
        if not token:
            raise _unauthorized("missing", request)
        try:
            async with request.app.state.sessionmaker() as session:
                verified = await ApiKeyService(session).verify(token)
        except AuthFailure as failure:
            raise _unauthorized(failure.reason, request) from None
        structlog.contextvars.bind_contextvars(
            api_key_prefix=verified.prefix, api_key_name=verified.name
        )
        log.info("api_request", method=request.method, path=request.url.path)
        return Principal(verified.name, verified.prefix, verified.scopes), f"key:{verified.id}"


READ = Depends(ScopeGuard(Scope.READ))
WRITE = Depends(ScopeGuard(Scope.WRITE))
ADMIN = Depends(ScopeGuard(Scope.ADMIN))
# WRITE everywhere, and also open to a public demo's visitor.
DEMO_WRITE = Depends(ScopeGuard(Scope.WRITE, public_demo=True))
