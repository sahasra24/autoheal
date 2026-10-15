from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from autoheal_control.cluster import NoCluster


def test_overview_aggregates_cluster_state(client: TestClient) -> None:
    ov = client.get("/api/v1/overview").json()
    assert ov["namespace"] == "autoheal"
    assert ov["healthy"] is True
    assert ov["pods_total"] == 3 and ov["pods_ready"] == 2
    assert ov["total_restarts"] == 2
    assert ov["recent_recovery_events"] == 1  # "Pulled" is not a recovery event
    assert ov["deployments"][0]["healthy"] is True


def test_overview_unhealthy_when_replicas_missing(client: TestClient, cluster) -> None:
    cluster.deployments[0].ready = 1
    assert client.get("/api/v1/overview").json()["healthy"] is False


def test_pods_filter_by_component(client: TestClient) -> None:
    names = [p["name"] for p in client.get("/api/v1/pods", params={"component": "tasks"}).json()]
    assert names == ["tasks-a", "tasks-b"]


def test_recovery_timeline_merges_events_and_terminations(client: TestClient) -> None:
    timeline = client.get("/api/v1/recovery").json()
    reasons = [(e["reason"], e["object_name"]) for e in timeline]
    assert reasons == [("Unhealthy", "tasks-a"), ("OOMKilled", "tasks-b")]
    assert timeline[1]["category"] == "oom"


def test_events_respects_limit(client: TestClient) -> None:
    assert len(client.get("/api/v1/events", params={"limit": 1}).json()) == 1
    assert client.get("/api/v1/events", params={"limit": 0}).status_code == 422


def test_no_cluster_backend_returns_503(make_client: Callable[..., TestClient]) -> None:
    client = make_client(cluster=NoCluster("autoheal"))
    resp = client.get("/api/v1/overview")
    assert resp.status_code == 503 and "CLUSTER_BACKEND=none" in resp.json()["detail"]
    assert client.get("/health/ready").json()["status"] == "degraded"  # non-critical
    assert client.get("/health/live").status_code == 200


def test_dashboard_served(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200 and "AutoHeal" in resp.text


# --- failure injection ---------------------------------------------------


def test_experiments_require_token(client: TestClient) -> None:
    assert client.post("/api/v1/chaos/experiments", json={"scenario": "crash"}).status_code == 401


def test_chaos_routes_absent_when_disabled(make_client: Callable[..., TestClient], auth) -> None:
    client = make_client(environment="production")  # chaos can never be active in production
    assert client.post("/api/v1/chaos/experiments", json={"scenario": "crash"}, headers=auth).status_code == 404
    assert client.get("/api/v1/info").json()["chaos_enabled"] is False


def test_crash_targets_a_ready_pod_directly(client: TestClient, pods, auth) -> None:
    resp = client.post(
        "/api/v1/chaos/experiments",
        json={"scenario": "crash", "target": "tasks", "pod": "tasks-b", "params": {"exit_code": 3}},
        headers=auth,
    )
    assert resp.status_code == 201
    exp = resp.json()
    assert exp["status"] == "injected" and exp["pod"] == "tasks-b" and exp["restarts_before"] == 2
    assert "kubelet restarts" in exp["expected_recovery"]
    call = pods.calls[-1]
    assert str(call.url) == "http://10.0.0.2:8000/chaos/crash"
    assert call.headers["X-Chaos-Token"] == "control-test-token"


def test_random_pod_selection_skips_unready_pods(client: TestClient, pods, auth) -> None:
    for _ in range(10):
        client.post("/api/v1/chaos/experiments", json={"scenario": "unready"}, headers=auth)
    assert {c.url.host for c in pods.calls} <= {"10.0.0.1", "10.0.0.2"}


def test_no_ready_pods_is_conflict(client: TestClient, auth) -> None:
    resp = client.post("/api/v1/chaos/experiments", json={"scenario": "crash", "target": "gateway"}, headers=auth)
    assert resp.status_code == 409


def test_target_allow_list_enforced(client: TestClient, auth) -> None:
    resp = client.post("/api/v1/chaos/experiments", json={"scenario": "crash", "target": "redis"}, headers=auth)
    assert resp.status_code == 403
    resp = client.post("/api/v1/chaos/experiments", json={"scenario": "pod_delete", "target": "kube-dns"}, headers=auth)
    assert resp.status_code == 403


def test_params_validated_per_scenario(client: TestClient, auth) -> None:
    resp = client.post(
        "/api/v1/chaos/experiments", json={"scenario": "memory_leak", "params": {"mb": 999999}}, headers=auth
    )
    assert resp.status_code == 422


def test_pod_delete_uses_kubernetes_api(client: TestClient, cluster, pods, auth) -> None:
    resp = client.post(
        "/api/v1/chaos/experiments", json={"scenario": "pod_delete", "target": "tasks", "pod": "tasks-a"}, headers=auth
    )
    assert resp.json()["status"] == "injected"
    assert cluster.deleted == ["tasks-a"]
    assert pods.calls == []


def test_target_rejection_recorded_as_failed(client: TestClient, pods, auth) -> None:
    pods.status = 401
    exp = client.post("/api/v1/chaos/experiments", json={"scenario": "crash"}, headers=auth).json()
    assert exp["status"] == "failed" and "401" in exp["detail"]
    history = client.get("/api/v1/chaos/experiments", headers=auth).json()
    assert history[0]["id"] == exp["id"]


def test_direct_targets_used_in_compose_mode(make_client: Callable[..., TestClient], pods, auth) -> None:
    client = make_client(cluster=NoCluster("local"), chaos_direct_targets="tasks=http://tasks:8000")
    exp = client.post("/api/v1/chaos/experiments", json={"scenario": "unhealthy"}, headers=auth).json()
    assert exp["status"] == "injected"
    assert str(pods.calls[-1].url) == "http://tasks:8000/chaos/unhealthy"


def test_scenarios_catalog(client: TestClient, auth) -> None:
    catalog = {s["scenario"]: s for s in client.get("/api/v1/chaos/scenarios", headers=auth).json()}
    assert set(catalog) >= {"crash", "unhealthy", "unready", "cpu_stress", "memory_leak", "pod_delete"}
    assert "redis" in catalog["pod_delete"]["allowed_targets"]
