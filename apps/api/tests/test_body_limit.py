"""Security review F-A1: a request body over the limit is refused with 413 before it is
parsed. Runs the real middleware on a tiny app; no database."""

import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agenteval_api.middleware import BodyLimitMiddleware

LIMIT = 1024


def make_client() -> tuple[TestClient, list[int]]:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, max_bytes=LIMIT)
    reads: list[int] = []

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        reads.append(len(body))
        return {"size": len(body)}

    return TestClient(app), reads


def test_a_body_within_the_limit_passes() -> None:
    client, reads = make_client()
    r = client.post("/echo", content=b"x" * LIMIT)
    assert r.status_code == 200 and r.json() == {"size": LIMIT}
    assert reads == [LIMIT]


def test_a_declared_oversize_body_is_refused_without_reading_it() -> None:
    client, reads = make_client()
    r = client.post("/echo", content=b"x" * (LIMIT + 1))
    assert r.status_code == 413
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["status"] == 413
    assert reads == []  # the handler never ran


def test_a_chunked_oversize_body_is_cut_off() -> None:
    client, reads = make_client()

    def chunks():  # no Content-Length: the size is only known by counting
        for _ in range(8):
            yield b"x" * 512

    r = client.post("/echo", content=chunks())
    assert r.status_code == 413
    assert reads == []


def test_get_requests_are_untouched() -> None:
    client, _ = make_client()
    assert client.get("/echo").status_code == 405  # reached the router, not refused with 413


def test_the_413_body_is_problem_json() -> None:
    client, _ = make_client()
    r = client.post("/echo", content=json.dumps({"a": "x" * 4096}))
    assert r.status_code == 413
    assert set(r.json()) == {"type", "title", "status", "detail"}
