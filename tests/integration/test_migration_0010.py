"""Migration 0010 (api_keys): the table and its constraints, and a downgrade that refuses
while any key exists. Schema drift against the models is covered by test_migrations."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.test_migration_0003 import scalar
from tests.integration.test_migrations import migrate


@pytest.fixture
async def at_0010(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0010")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def insert_key(engine: AsyncEngine, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "name": "ci",
        "prefix": "0a1b2c3d",
        "digest": b"\x00" * 32,
        "scopes": ["read"],
        **overrides,
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO api_keys (id, name, prefix, secret_sha256, scopes)"
                " VALUES (gen_random_uuid(), :name, :prefix, :digest, :scopes)"
            ),
            values,
        )


async def test_upgrade_creates_an_empty_table_with_its_constraints(
    at_0010: tuple[str, AsyncEngine],
) -> None:
    _, engine = at_0010
    assert await scalar(engine, "SELECT count(*) FROM api_keys") == 0
    names = await scalar(
        engine,
        "SELECT string_agg(conname, ',' ORDER BY conname) FROM pg_constraint"
        " WHERE conrelid = 'api_keys'::regclass",
    )
    assert names == (
        "ck_api_keys_prefix_format,ck_api_keys_scopes,ck_api_keys_secret_sha256_len,"
        "pk_api_keys,uq_api_keys_prefix"
    )


async def test_constraints(at_0010: tuple[str, AsyncEngine]) -> None:
    _, engine = at_0010
    await insert_key(engine, scopes=["read", "write", "admin"])
    with pytest.raises(IntegrityError, match="uq_api_keys_prefix"):
        await insert_key(engine)
    for bad_prefix in ("0A1B2C3D", "0a1b2c3", "zzzzzzzz"):
        with pytest.raises(IntegrityError, match="ck_api_keys_prefix_format"):
            await insert_key(engine, prefix=bad_prefix)
    for bad_scopes in ([], ["root"], ["read", "root"]):
        with pytest.raises(IntegrityError, match="ck_api_keys_scopes"):
            await insert_key(engine, prefix="11111111", scopes=bad_scopes)
    for bad_digest in (b"", b"\x00" * 31, b"\x00" * 33):
        with pytest.raises(IntegrityError, match="ck_api_keys_secret_sha256_len"):
            await insert_key(engine, prefix="22222222", digest=bad_digest)


async def test_downgrade_drops_the_empty_table_and_upgrade_restores_it(
    at_0010: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0010
    await migrate(url, "downgrade", "0009")
    assert await scalar(engine, "SELECT to_regclass('api_keys') IS NULL")
    await migrate(url, "upgrade", "0010")
    assert await scalar(engine, "SELECT count(*) FROM api_keys") == 0


async def test_downgrade_refuses_while_a_key_exists(at_0010: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0010
    await insert_key(engine)
    with pytest.raises(RuntimeError, match="refusing to downgrade 0010: 1 API key"):
        await migrate(url, "downgrade", "0009")
    assert await scalar(engine, "SELECT count(*) FROM api_keys") == 1  # nothing was dropped

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM api_keys"))
    await migrate(url, "downgrade", "0009")
    assert await scalar(engine, "SELECT to_regclass('api_keys') IS NULL")
