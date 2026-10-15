from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

JOB = {"type": "prime_count", "params": {"limit": 1000}}


def test_root_and_health(client: TestClient) -> None:
    assert client.get("/").json()["service"] == "autoheal-gateway"
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_submit_job_is_forwarded_with_correlation_id(client: TestClient, backends) -> None:
    resp = client.post("/api/v1/jobs", json=JOB, headers={"X-Correlation-ID": "corr-0001-abcd"})
    assert resp.status_code == 202
    assert resp.headers["Location"].startswith("/api/v1/jobs/")
    assert resp.headers["X-Correlation-ID"] == "corr-0001-abcd"
    upstream = backends.calls[-1]
    assert upstream.url == "http://tasks/api/v1/jobs"
    assert upstream.headers["X-Correlation-ID"] == "corr-0001-abcd"


def test_correlation_id_generated_when_missing_or_malformed(client: TestClient, backends) -> None:
    resp = client.get("/api/v1/queue", headers={"X-Correlation-ID": "bad id with spaces"})
    generated = resp.headers["X-Correlation-ID"]
    assert generated != "bad id with spaces" and len(generated) == 32
    assert backends.calls[-1].headers["X-Correlation-ID"] == generated


def test_invalid_payload_rejected_at_edge_without_upstream_call(client: TestClient, backends) -> None:
    assert client.post("/api/v1/jobs", json={"type": "prime_count", "params": {"limit": -1}}).status_code == 422
    assert client.post("/api/v1/jobs", json={"type": "nope"}).status_code == 422
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 422
    assert client.get("/api/v1/jobs", params={"limit": 9999}).status_code == 422
    assert backends.calls == []


def test_query_params_are_forwarded(client: TestClient, backends) -> None:
    client.get("/api/v1/jobs", params={"limit": 7, "status": "failed"})
    assert dict(backends.calls[-1].url.params) == {"limit": "7", "status": "failed"}


def test_rate_limit_returns_429_with_headers(client: TestClient) -> None:
    statuses = []
    for _ in range(6):
        resp = client.get("/api/v1/queue")
        statuses.append(resp.status_code)
    assert statuses == [200] * 5 + [429]
    assert resp.headers["X-RateLimit-Limit"] == "5"
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert int(resp.headers["Retry-After"]) >= 1


def test_rate_limit_headers_on_success_and_health_is_exempt(client: TestClient) -> None:
    resp = client.get("/api/v1/queue")
    assert resp.headers["X-RateLimit-Remaining"] == "4"
    for _ in range(20):
        assert client.get("/health/live").status_code == 200


def test_api_keys_enforced_when_configured(make_client: Callable[..., TestClient]) -> None:
    client = make_client(api_keys="alpha,beta")
    assert client.get("/api/v1/queue").status_code == 401
    assert client.get("/api/v1/queue", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/queue", headers={"X-API-Key": "beta"}).status_code == 200
    assert client.get("/health/ready").status_code == 200  # probes never need a key


def test_body_size_limit(make_client: Callable[..., TestClient]) -> None:
    client = make_client(max_body_bytes=50)
    resp = client.post("/api/v1/jobs", json={"type": "sleep", "params": {"seconds": 1}, "pad": "x" * 100})
    assert resp.status_code == 413


def test_connect_failure_returns_503_and_opens_circuit(client: TestClient, backends) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backends.handler = down
    assert client.post("/api/v1/jobs", json=JOB).status_code == 503
    assert client.post("/api/v1/jobs", json=JOB).status_code == 503
    calls_before = len(backends.calls)
    resp = client.post("/api/v1/jobs", json=JOB)  # breaker is now open: fail fast
    assert resp.status_code == 503 and "circuit open" in resp.json()["detail"]
    assert "Retry-After" in resp.headers
    assert len(backends.calls) == calls_before


def test_get_retries_once_on_connect_error(client: TestClient, backends) -> None:
    attempts = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("pod went away", request=request)
        return httpx.Response(200, json={"queued": 0, "processing": 0})

    backends.handler = flaky
    assert client.get("/api/v1/queue").status_code == 200
    assert len(attempts) == 2


def test_timeout_returns_504(client: TestClient, backends) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    backends.handler = slow
    assert client.get("/api/v1/queue").status_code == 504


def test_cluster_views_proxy_to_control(client: TestClient, backends) -> None:
    resp = client.get("/api/v1/cluster/pods", params={"limit": 10})
    assert resp.status_code == 200
    assert backends.calls[-1].url == "http://control/api/v1/pods?limit=10"
    assert client.get("/api/v1/cluster/secrets").status_code == 422


def test_chaos_not_reachable_through_gateway(client: TestClient) -> None:
    assert client.post("/api/v1/cluster/chaos").status_code == 405
    assert client.post("/chaos/crash").status_code == 404
