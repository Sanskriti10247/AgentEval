"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, pool_size: int = 5, max_overflow: int = 10) -> AsyncEngine:
    return create_async_engine(
        database_url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False so returned ORM objects stay readable after commit.
    return async_sessionmaker(engine, expire_on_commit=False)
