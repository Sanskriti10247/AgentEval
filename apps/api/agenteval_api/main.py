from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from agenteval_api.errors import install_error_handlers
from agenteval_api.middleware import MetricsMiddleware
from agenteval_api.routes import (
    agents,
    datasets,
    evaluations,
    health,
    metrics,
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
        app.state.engine = create_engine(settings.database_url)
        app.state.sessionmaker = create_sessionmaker(app.state.engine)
        app.state.redis = Redis.from_url(settings.redis_url)
        app.state.queue = RunQueue(
            app.state.redis,
            settings.dispatch_stream,
            settings.consumer_group,
            maxlen=settings.stream_maxlen,
        )
        log.info("api_started")
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()
            tracing.shutdown()

    app = FastAPI(title="AgentEval API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(MetricsMiddleware)
    install_error_handlers(app)
    # The dashboard's origin(s) only; no credentials (there is no auth yet), and
    # just the methods and headers the API uses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
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
    app.include_router(v1)
    return app


app = create_app()
