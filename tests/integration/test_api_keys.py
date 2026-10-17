"""API keys (ADR-012 A): the service lifecycle against real Postgres, and the CLI."""

import io
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_api import keys as keys_cli
from agenteval_core import auth
from agenteval_core.auth import AuthFailure, Scope
from agenteval_core.config import Settings
from agenteval_core.services.api_keys import ApiKeyService
from agenteval_core.services.errors import InvalidApiKeyRequest, NotFound

SM = async_sessionmaker[AsyncSession]
KEY_RE = re.compile(r"ae_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


async def make_key(sm: SM, scopes: list[Scope] | None = None, **kw: object) -> tuple[str, str]:
    async with sm() as s:
        created = await ApiKeyService(s).create("k", scopes or [Scope.READ], **kw)  # type: ignore[arg-type]
    return created.plaintext, created.key.prefix


async def verify(sm: SM, plaintext: str, **kw: object) -> object:
    async with sm() as s:
        return await ApiKeyService(s).verify(plaintext, **kw)  # type: ignore[arg-type]


async def test_create_stores_only_a_digest_and_returns_the_plaintext_once(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    plaintext, prefix = await make_key(sessionmaker, [Scope.WRITE, Scope.READ])
    assert KEY_RE.fullmatch(plaintext) and plaintext[3:11] == prefix
    secret = plaintext[12:]
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT to_jsonb(k) AS j FROM api_keys k WHERE prefix = :p"), {"p": prefix}
            )
        ).scalar_one()
    assert secret not in str(row) and plaintext not in str(row)
    assert row["scopes"] == ["read", "write"] and row["revoked_at"] is None
    assert row["secret_sha256"] == "\\x" + auth.hash_secret(secret).hex()
    async with sessionmaker() as s:
        listed = await ApiKeyService(s).list_all()
    assert [k.prefix for k in listed] == [prefix]
    assert not hasattr(listed[0], "key") and secret not in repr(vars(listed[0]))


async def test_verify_accepts_a_valid_key_and_reports_its_scopes(sessionmaker: SM) -> None:
    plaintext, prefix = await make_key(sessionmaker, [Scope.READ, Scope.ADMIN])
    ok = await verify(sessionmaker, plaintext)
    assert (ok.prefix, ok.name, ok.scopes) == (prefix, "k", {Scope.READ, Scope.ADMIN})  # type: ignore[attr-defined]


async def test_verify_rejects_wrong_unknown_and_malformed_keys(sessionmaker: SM) -> None:
    plaintext, prefix = await make_key(sessionmaker)
    wrong_secret = f"ae_{prefix}_" + "A" * 43
    unknown_prefix = "ae_ffffffff_" + plaintext[12:]
    for bad in (wrong_secret, unknown_prefix, "ae_nope", "", plaintext + "x", plaintext[:-1]):
        with pytest.raises(AuthFailure) as exc:
            await verify(sessionmaker, bad)
        assert exc.value.reason == "invalid", bad


async def test_verify_compares_digests_in_constant_time_even_for_an_unknown_prefix(
    sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    plaintext, _ = await make_key(sessionmaker)
    calls: list[tuple[bytes, bytes]] = []
    real = auth.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    await verify(sessionmaker, plaintext)
    assert len(calls) == 1 and calls[0][0] == auth.hash_secret(plaintext[12:])
    with pytest.raises(AuthFailure):
        await verify(sessionmaker, "ae_ffffffff_" + plaintext[12:])
    assert len(calls) == 2  # a miss costs the same comparison as a hit


async def test_revoked_and_expired_keys_are_rejected_with_their_reason(sessionmaker: SM) -> None:
    plaintext, prefix = await make_key(sessionmaker)
    async with sessionmaker() as s:
        await ApiKeyService(s).revoke(prefix)
    with pytest.raises(AuthFailure) as exc:
        await verify(sessionmaker, plaintext)
    assert exc.value.reason == "revoked"

    short, _ = await make_key(sessionmaker, expires_days=1)
    await verify(sessionmaker, short)  # still valid now
    with pytest.raises(AuthFailure) as exc2:
        await verify(sessionmaker, short, now=datetime.now(UTC) + timedelta(days=1, seconds=5))
    assert exc2.value.reason == "expired"


async def test_a_revoked_or_expired_reason_is_never_given_for_a_wrong_secret(
    sessionmaker: SM,
) -> None:
    plaintext, prefix = await make_key(sessionmaker)
    async with sessionmaker() as s:
        await ApiKeyService(s).revoke(prefix)
    with pytest.raises(AuthFailure) as exc:
        await verify(sessionmaker, f"ae_{prefix}_" + "B" * 43)
    assert exc.value.reason == "invalid"  # does not confirm the prefix exists


async def test_revoke_is_idempotent_and_unknown_prefixes_are_404(sessionmaker: SM) -> None:
    _, prefix = await make_key(sessionmaker)
    async with sessionmaker() as s:
        first = await ApiKeyService(s).revoke(prefix)
    async with sessionmaker() as s:
        again = await ApiKeyService(s).revoke(prefix)
    assert first.revoked_at is not None and again.revoked_at == first.revoked_at
    for bad in ("ffffffff", "nothex!!", ""):
        async with sessionmaker() as s:
            with pytest.raises(NotFound):
                await ApiKeyService(s).revoke(bad)


async def test_create_validates_its_arguments(sessionmaker: SM) -> None:
    async with sessionmaker() as s:
        svc = ApiKeyService(s)
        for name, scopes, days in (("  ", [Scope.READ], None), ("x" * 101, [Scope.READ], None),
                                   ("ok", [], None), ("ok", [Scope.READ], 0)):  # fmt: skip
            with pytest.raises(InvalidApiKeyRequest):
                await svc.create(name, scopes, days)


async def test_last_used_at_is_written_at_most_once_a_minute(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    plaintext, prefix = await make_key(sessionmaker)

    async def last_used() -> datetime | None:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT last_used_at FROM api_keys WHERE prefix = :p"), {"p": prefix}
                )
            ).scalar_one()

    assert await last_used() is None
    await verify(sessionmaker, plaintext)
    first = await last_used()
    assert first is not None
    for _ in range(5):
        await verify(sessionmaker, plaintext)
    assert await last_used() == first  # throttled: no write

    async with engine.begin() as conn:  # pretend the last write was two minutes ago
        await conn.execute(
            text(
                "UPDATE api_keys SET last_used_at = now() - interval '2 minutes' WHERE prefix = :p"
            ),
            {"p": prefix},
        )
    await verify(sessionmaker, plaintext)
    refreshed = await last_used()
    assert refreshed is not None and refreshed > first - timedelta(minutes=1)
    assert datetime.now(UTC) - refreshed < timedelta(seconds=30)


# -- the CLI -----------------------------------------------------------------------------------


async def cli(settings: Settings, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = await keys_cli.run(argv, settings, out)
    return code, out.getvalue()


async def test_cli_create_prints_the_key_once_and_it_authenticates(
    settings: Settings, sessionmaker: SM
) -> None:
    code, out = await cli(
        settings, "create", "--name", "ci bot", "--scopes", "write,read", "--expires-days", "7"
    )
    assert code == 0
    found = KEY_RE.findall(out)
    assert len(found) == 1
    ok = await verify(sessionmaker, found[0])
    assert ok.scopes == {Scope.READ, Scope.WRITE} and ok.name == "ci bot"  # type: ignore[attr-defined]
    assert "scopes read,write" in out and "cannot be shown again" in out

    code, listing = await cli(settings, "list")
    assert code == 0 and found[0][3:11] in listing and "ci bot" in listing
    assert found[0] not in listing and found[0][12:] not in listing  # never the secret


async def test_cli_revoke_and_errors(settings: Settings, sessionmaker: SM) -> None:
    _, out = await cli(settings, "create", "--name", "x", "--scopes", "admin")
    key = KEY_RE.findall(out)[0]
    code, msg = await cli(settings, "revoke", key[3:11])
    assert code == 0 and "Revoked" in msg
    with pytest.raises(AuthFailure) as exc:
        await verify(sessionmaker, key)
    assert exc.value.reason == "revoked"
    _, listing = await cli(settings, "list")
    assert "revoked" in listing

    code, _ = await cli(settings, "revoke", "ffffffff")
    assert code == 1
    for argv in (["create", "--name", "x", "--scopes", "root"], ["create", "--scopes", "read"], []):
        with pytest.raises(SystemExit) as exit_:
            await cli(settings, *argv)
        assert exit_.value.code == 2
