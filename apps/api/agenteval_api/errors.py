"""Maps service errors to RFC 7807-style problem responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agenteval_core.services.errors import (
    Conflict,
    DispatchUnavailable,
    IdempotencyConflict,
    InvalidAgentVersion,
    InvalidRun,
    InvalidTask,
    NotFound,
    ServiceError,
)

_STATUS: dict[type[ServiceError], tuple[int, str]] = {
    NotFound: (404, "Not Found"),
    InvalidTask: (422, "Invalid Task"),
    InvalidAgentVersion: (422, "Invalid Agent Version"),
    InvalidRun: (422, "Invalid Run"),
    IdempotencyConflict: (409, "Idempotency Conflict"),
    Conflict: (409, "Conflict"),
    DispatchUnavailable: (503, "Dispatch Unavailable"),
}


def _status_for(exc: ServiceError) -> tuple[int, str]:
    for cls in type(exc).__mro__:  # subclasses inherit their parent's status
        if cls in _STATUS:
            return _STATUS[cls]
    return 500, "Internal Server Error"


def problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
        media_type="application/problem+json",
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        status, title = _status_for(exc)
        resp = problem(status, title, str(exc))
        if isinstance(exc, DispatchUnavailable):
            resp.headers["Retry-After"] = "1"
        return resp
