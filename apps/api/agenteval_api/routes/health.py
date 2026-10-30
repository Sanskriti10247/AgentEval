from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from agenteval_api import demo

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness: the process is up. For the dashboard: `read_only` (no writes at all, no
    API key needed to browse), `public_demo` (no API key; only launching evaluations and
    runs, cancel and replay are accepted) and `demo_limits` (that mode's caps, else null)."""
    settings = request.app.state.settings
    return {
        "status": "ok",
        "read_only": settings.read_only,
        "public_demo": demo.is_public_demo(settings),
        "demo_limits": demo.limits(settings),
    }


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: Postgres and Redis are reachable."""
    checks: dict[str, str] = {}
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report any failure as not ready
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})
