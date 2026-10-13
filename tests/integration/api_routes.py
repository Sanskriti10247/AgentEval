"""Enumerate every API route with its full path. FastAPI 0.141 nests
included routers lazily (_IncludedRouter), so app.routes alone misses them."""

from collections.abc import Iterator, Sequence
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute


def api_routes(app: FastAPI) -> Iterator[tuple[str, str, APIRoute]]:
    """(METHOD, full path, route) for every APIRoute."""

    def walk(routes: Sequence[Any], prefix: str) -> Iterator[tuple[str, str, APIRoute]]:
        for r in routes:
            if isinstance(r, APIRoute):
                for method in sorted(r.methods):
                    yield method, prefix + r.path, r
            elif hasattr(r, "original_router"):  # an included router
                yield from walk(r.original_router.routes, prefix + r.include_context.prefix)

    yield from walk(app.routes, "")
