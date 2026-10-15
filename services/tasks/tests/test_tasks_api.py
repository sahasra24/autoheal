from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient


def wait_for_status(client: TestClient, job_id: str, wanted: set[str], timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in wanted:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {wanted}; last={job}")


def test_submit_and_complete_cpu_job(client: TestClient) -> None:
    resp = client.post("/api/v1/jobs", json={"type": "prime_count", "params": {"limit": 1000}})
    assert resp.status_code == 202
    job = resp.json()
    assert resp.headers["Location"] == f"/api/v1/jobs/{job['id']}"
    done = wait_for_status(client, job["id"], {"succeeded"})
    assert done["result"] == {"limit": 1000, "primes": 168}
    assert done["worker"] == "tasks-test-0"


def test_correlation_id_is_stored_on_job(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/jobs", json={"type": "sleep", "params": {"seconds": 0}}, headers={"X-Correlation-ID": "trace-abc-123"}
    )
    assert resp.headers["X-Correlation-ID"] == "trace-abc-123"
    assert resp.json()["correlation_id"] == "trace-abc-123"


def test_failing_job_is_retried_then_failed(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/jobs", json={"type": "flaky", "params": {"failure_rate": 1, "seconds": 0}, "max_attempts": 2}
    )
    done = wait_for_status(client, resp.json()["id"], {"failed"})
    assert done["attempts"] == 2
    assert "simulated transient failure" in done["error"]


def test_validation_rejects_bad_payloads(client: TestClient) -> None:
    assert client.post("/api/v1/jobs", json={"type": "rm -rf"}).status_code == 422
    assert client.post("/api/v1/jobs", json={"type": "prime_count", "params": {"limit": 10**9}}).status_code == 422
    assert client.post("/api/v1/jobs", json={"type": "sleep", "params": {"seconds": 1, "x": 1}}).status_code == 422


def test_unknown_job_is_404_and_malformed_id_is_422(client: TestClient) -> None:
    assert client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 422


def test_list_jobs_filters_by_status(client: TestClient) -> None:
    job_id = client.post("/api/v1/jobs", json={"type": "sleep", "params": {"seconds": 0}}).json()["id"]
    wait_for_status(client, job_id, {"succeeded"})
    listed = client.get("/api/v1/jobs", params={"status": "succeeded"}).json()
    assert job_id in [j["id"] for j in listed["items"]]
    assert client.get("/api/v1/jobs", params={"status": "failed"}).json()["count"] == 0


def test_compute_endpoint(client: TestClient) -> None:
    body = client.post("/api/v1/compute", json={"iterations": 10}).json()
    assert body["iterations"] == 10 and len(body["digest"]) == 64 and body["pod"] == "tasks-test-0"


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/health/startup").status_code == 200
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["redis"]["ok"] is True


def test_metrics_exposes_job_counters(client: TestClient) -> None:
    client.post("/api/v1/jobs", json={"type": "sleep", "params": {"seconds": 0}})
    text = client.get("/metrics").text
    assert "autoheal_jobs_submitted_total" in text
    assert 'route="/api/v1/jobs"' in text


# --- failure injection ---------------------------------------------------


def test_chaos_requires_token(client: TestClient, chaos: dict[str, str]) -> None:
    assert client.post("/chaos/unhealthy").status_code == 401
    assert client.post("/chaos/unhealthy", headers={"X-Chaos-Token": "wrong"}).status_code == 401


def test_chaos_unhealthy_fails_liveness_only(client: TestClient, chaos: dict[str, str]) -> None:
    assert client.post("/chaos/unhealthy", headers=chaos).status_code == 202
    assert client.get("/health/live").status_code == 503
    assert client.get("/health/ready").status_code == 200  # liveness and readiness are independent
    client.post("/chaos/reset", headers=chaos)
    assert client.get("/health/live").status_code == 200


def test_chaos_unready_fails_readiness_only(client: TestClient, chaos: dict[str, str]) -> None:
    client.post("/chaos/unready", json={"seconds": 30}, headers=chaos)
    ready = client.get("/health/ready")
    assert ready.status_code == 503 and "failure injection" in ready.json()["reason"]
    assert client.get("/health/live").status_code == 200


def test_chaos_crash_exits_process(client: TestClient, exits: list[int], chaos: dict[str, str]) -> None:
    assert client.post("/chaos/crash", json={"exit_code": 137, "delay_seconds": 0}, headers=chaos).status_code == 202
    deadline = time.monotonic() + 2
    while not exits and time.monotonic() < deadline:
        time.sleep(0.02)
    assert exits == [137]


def test_chaos_error_rate_affects_api_but_not_probes(client: TestClient, chaos: dict[str, str]) -> None:
    client.post("/chaos/errors", json={"rate": 1.0}, headers=chaos)
    assert client.get("/api/v1/queue").status_code == 503
    assert client.get("/health/live").status_code == 200
    client.post("/chaos/reset", headers=chaos)
    assert client.get("/api/v1/queue").status_code == 200


def test_chaos_memory_allocation_is_reported(client: TestClient, chaos: dict[str, str]) -> None:
    client.post("/chaos/memory", json={"mb": 2}, headers=chaos)
    assert client.get("/chaos", headers=chaos).json()["memory_mb"] == 2
