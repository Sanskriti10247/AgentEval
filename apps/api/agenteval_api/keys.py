"""Operator CLI for API keys (ADR-012 A). Run it where the database is reachable,
normally inside the API container:

    docker compose exec api python -m agenteval_api.keys create --name ci --scopes read,write
    docker compose exec api python -m agenteval_api.keys list
    docker compose exec api python -m agenteval_api.keys revoke <prefix>

`create` prints the key ONCE; only its SHA-256 is stored, so it cannot be shown again.
This is the only way to mint the first (admin) key: there is no anonymous endpoint.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TextIO

from agenteval_core import auth
from agenteval_core.config import Settings, get_settings
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.models import ApiKey
from agenteval_core.services.api_keys import ApiKeyService
from agenteval_core.services.errors import ServiceError


def _scopes(text: str) -> list[auth.Scope]:
    try:
        return auth.parse_scopes(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agenteval_api.keys", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a key and print it once")
    create.add_argument("--name", required=True, help="label for the key's holder")
    create.add_argument(
        "--scopes", type=_scopes, required=True, help="comma list of read,write,admin"
    )
    create.add_argument("--expires-days", type=int, default=None, help="default: never expires")
    sub.add_parser("list", help="list keys (never the secret)")
    revoke = sub.add_parser("revoke", help="revoke a key by its prefix")
    revoke.add_argument("prefix")
    return parser


def _state(key: ApiKey) -> str:
    return "revoked" if key.revoked_at else "active"


def _when(value: object) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else "-"


async def run(
    argv: Sequence[str], settings: Settings | None = None, out: TextIO = sys.stdout
) -> int:
    """Execute one command; returns the process exit code. Async so tests can await it."""
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            service = ApiKeyService(session)
            try:
                if args.command == "create":
                    created = await service.create(args.name, args.scopes, args.expires_days)
                    k = created.key
                    expires = _when(k.expires_at) if k.expires_at else "never"
                    out.write(
                        f"Created API key {k.name!r} (prefix {k.prefix}, scopes "
                        f"{','.join(k.scopes)}, expires {expires}).\n"
                        "Store it now; it cannot be shown again:\n"
                        f"{created.plaintext}\n"
                    )
                elif args.command == "list":
                    keys = await service.list_all()
                    head = ("PREFIX", "NAME", "SCOPES", "STATE", "LAST USED")
                    out.write(
                        f"{head[0]:<10}{head[1]:<24}{head[2]:<18}{head[3]:<9}{head[4]:<18}EXPIRES\n"
                    )
                    for k in keys:
                        out.write(
                            f"{k.prefix:<10}{k.name[:22]:<24}{','.join(k.scopes):<18}"
                            f"{_state(k):<9}{_when(k.last_used_at):<18}{_when(k.expires_at)}\n"
                        )
                else:
                    k = await service.revoke(args.prefix)
                    out.write(f"Revoked API key {k.prefix} ({k.name!r}).\n")
            except ServiceError as exc:
                sys.stderr.write(f"error: {exc}\n")
                return 1
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    sys.exit(asyncio.run(run(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
