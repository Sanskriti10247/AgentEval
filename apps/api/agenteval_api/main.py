from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from agenteval_api.errors import install_error_handlers
from agenteval_api.middleware import BodyLimitMiddleware, MetricsMiddleware
from agenteval_api.ratelimit import RateLimiter
from agenteval_api.routes import (
    agents,
    datasets,
    evaluations,
    health,
    keys,
    metrics,
    providers,
    runs,
    stats,
    tasks,
    workers,
)
from agenteval_core.config import Settings, get_settings
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.logging import configure_logging, get_logger
from agenteval_core.observability.tracing import configure_tracing
from agenteval_core.queue.streams import RunQueue

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_json)
        tracing = configure_tracing("agenteval-api")
        app.state.settings = settings
        app.state.engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        app.state.sessionmaker = create_sessionmaker(app.state.engine)
        app.state.redis = Redis.from_url(settings.redis_url)
        app.state.queue = RunQueue(
            app.state.redis,
            settings.dispatch_stream,
            settings.consumer_group,
            maxlen=settings.stream_maxlen,
        )
        app.state.ratelimiter = RateLimiter(app.state.redis)
        if settings.read_only:
            log.warning("read_only", note="writes answer 403; reads need no API key")
        elif settings.public_demo:
            log.warning(
                "public_demo",
                note="no API key; only evaluations, runs, cancel and replay are writable",
                max_repetitions=settings.demo_max_repetitions,
                max_active_runs=settings.demo_max_active_runs,
            )
        elif not settings.auth_enabled:
            log.warning("auth_disabled", note="every caller is anonymous with all scopes")
        log.info(
            "api_started",
            auth_enabled=settings.auth_enabled,
            read_only=settings.read_only,
            public_demo=settings.public_demo,
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()
            tracing.shutdown()

    app = FastAPI(title="AgentEval API", version="0.1.0", lifespan=lifespan)
    # Innermost, so the 413 is still counted by the metrics middleware and gets CORS headers.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(MetricsMiddleware)
    install_error_handlers(app)
    # The dashboard's origin(s) only; no cookies (the key travels in the Authorization
    # header, ADR-012 D), and just the methods and headers the API uses. The rate-limit
    # headers are exposed so the dashboard can read Retry-After on a 429.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=[
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
        allow_credentials=False,
        max_age=600,
    )
    app.include_router(health.router)
    app.include_router(metrics.router)
    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(agents.router)
    v1.include_router(tasks.router)
    v1.include_router(runs.router)
    v1.include_router(datasets.router)
    v1.include_router(evaluations.router)
    v1.include_router(workers.router)
    v1.include_router(stats.router)
    v1.include_router(providers.router)
    v1.include_router(keys.router)
    app.include_router(v1)
    return app


app = create_app()
