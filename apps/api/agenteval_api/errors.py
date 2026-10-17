"""Maps service errors to RFC 7807-style problem responses, and sanitises
request validation errors (D11).

FastAPI's default 422 echoes the submitted value (`input`, `ctx`). A task's
evaluation spec carries hidden files, check names and expected values, so
every 422 here drops `input` and `ctx`, and an error inside `evaluation`
keeps only a truncated location and a fixed message: no hidden file name,
check name, pattern, expected value or submitted content can come back.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.services.errors import (
    Conflict,
    DispatchUnavailable,
    IdempotencyConflict,
    InvalidAgentVersion,
    InvalidCursor,
    InvalidEvaluation,
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
    InvalidCursor: (422, "Invalid Cursor"),
    InvalidEvaluation: (422, "Invalid Evaluation"),
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

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": [sanitize_error(e) for e in exc.errors()]},
        )


# Where a task's evaluation spec sits in a request: ("body", "evaluation", ...).
_EVALUATION_LOC = ("body", "evaluation")
_SPEC_FIELDS = frozenset(EvaluationSpec.model_fields)


def sanitize_error(error: dict[str, Any]) -> dict[str, Any]:
    """One validation error without the submitted value. Inside the
    evaluation spec, the location keeps at most a KNOWN spec field name and a
    list index - anything else there (dict keys are hidden file names; an
    unknown key is whatever was submitted) is dropped - and the message is
    fixed, because validator messages can quote check names and patterns."""
    loc = tuple(error.get("loc", ()))
    kind = str(error.get("type", "value_error"))
    if loc[:2] == _EVALUATION_LOC:
        kept: list[str | int] = list(_EVALUATION_LOC)
        if len(loc) > 2 and loc[2] in _SPEC_FIELDS:
            kept.append(loc[2])
            if len(loc) > 3 and isinstance(loc[3], int):
                kept.append(loc[3])
        return {"type": kind, "loc": kept, "msg": f"invalid evaluation spec ({kind})"}
    return {"type": kind, "loc": list(loc), "msg": str(error.get("msg", ""))}
