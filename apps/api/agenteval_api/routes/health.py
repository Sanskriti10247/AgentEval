from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, str | bool]:
    """Liveness: the process is up. `read_only` tells the dashboard whether this API
    accepts writes (and so whether it needs an API key to browse)."""
    return {"status": "ok", "read_only": request.app.state.settings.read_only}


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
