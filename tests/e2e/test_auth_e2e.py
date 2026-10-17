"""Authentication, scopes and key management through the running stack (ADR-012).

The rest of the e2e suite already runs with authentication ON (it sends a key); these
tests show the stack really refuses everything else.
"""

import httpx
import pytest

from tests.e2e.bench import API, BASE_URL, api_headers

pytestmark = pytest.mark.e2e


def test_requests_without_a_key_are_refused_but_probes_are_open(stack: httpx.Client) -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10) as anon:
        assert anon.get("/health").status_code == 200
        assert anon.get("/ready").status_code == 200
        assert anon.get("/metrics").status_code == 200
        resp = anon.get(f"{API}/runs")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
        assert anon.get(f"{API}/runs", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_read_only_key_browses_but_cannot_write_and_revocation_is_immediate(
    stack: httpx.Client,
) -> None:
    created = stack.post(f"{API}/keys", json={"name": "e2e-reader", "scopes": ["read"]})
    assert created.status_code == 201, created.text
    key, prefix = created.json()["key"], created.json()["prefix"]
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(base_url=BASE_URL, timeout=10, headers=headers) as reader:
            ok = reader.get(f"{API}/stats/overview")
            assert ok.status_code == 200
            assert int(ok.headers["x-ratelimit-limit"]) > 0  # budgets are active on the stack
            denied = reader.post(f"{API}/agents", json={"name": "never-created"})
            assert denied.status_code == 403
            assert "write scope" in denied.json()["detail"]
            assert reader.get(f"{API}/keys").status_code == 403
    finally:
        assert stack.delete(f"{API}/keys/{prefix}").status_code == 204
    with httpx.Client(base_url=BASE_URL, timeout=10, headers=headers) as revoked:
        assert revoked.get(f"{API}/stats/overview").status_code == 401


def test_the_suite_key_is_what_the_helper_returns(stack: httpx.Client) -> None:
    assert stack.headers["authorization"] == api_headers()["Authorization"]
