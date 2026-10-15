from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from autoheal_control.config import ControlSettings
from autoheal_control.main import create_app
from autoheal_control.models import (
    AutoscalerStatus,
    ClusterEvent,
    DeploymentStatus,
    EventCategory,
    PodStatus,
    Termination,
)

TOKEN = "control-test-token"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeCluster:
    namespace = "autoheal"

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.deployments = [
            DeploymentStatus(name="autoheal-tasks", component="tasks", desired=2, ready=2, available=2, updated=2),
            DeploymentStatus(name="autoheal-gateway", component="gateway", desired=2, ready=2, available=2, updated=2),
        ]
        self.pods = [
            PodStatus(name="tasks-a", component="tasks", phase="Running", ready=True, restarts=0, pod_ip="10.0.0.1"),
            PodStatus(
                name="tasks-b",
                component="tasks",
                phase="Running",
                ready=True,
                restarts=2,
                pod_ip="10.0.0.2",
                last_termination=Termination(reason="OOMKilled", exit_code=137, finished_at=NOW - timedelta(minutes=5)),
            ),
            PodStatus(
                name="gateway-a", component="gateway", phase="Running", ready=False, restarts=0, pod_ip="10.0.0.3"
            ),
        ]
        self.events = [
            ClusterEvent(
                timestamp=NOW,
                type="Warning",
                reason="Unhealthy",
                category=EventCategory.probe,
                object_kind="Pod",
                object_name="tasks-a",
                message="Liveness probe failed: HTTP probe failed with statuscode: 503",
            ),
            ClusterEvent(
                timestamp=NOW - timedelta(minutes=1),
                type="Normal",
                reason="Pulled",
                category=EventCategory.other,
                object_kind="Pod",
                object_name="tasks-a",
                message="Container image already present",
            ),
        ]

    async def list_deployments(self) -> list[DeploymentStatus]:
        return self.deployments

    async def list_pods(self, component: str | None = None) -> list[PodStatus]:
        return [p for p in self.pods if component is None or p.component == component]

    async def list_autoscalers(self) -> list[AutoscalerStatus]:
        return [
            AutoscalerStatus(
                name="autoheal-tasks",
                target="autoheal-tasks",
                min_replicas=2,
                max_replicas=10,
                current_replicas=2,
                desired_replicas=2,
                target_cpu_utilization=60,
                current_cpu_utilization=12,
            )
        ]

    async def list_events(self, limit: int) -> list[ClusterEvent]:
        return self.events[:limit]

    async def delete_pod(self, name: str) -> None:
        self.deleted.append(name)

    async def ping(self) -> None:
        return None


class FakePods:
    """Stands in for the per-pod /chaos endpoints."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status = 202

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(self.status, json={"kind": request.url.path})


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
def pods() -> FakePods:
    return FakePods()


@pytest.fixture
def make_client(cluster: FakeCluster, pods: FakePods) -> Iterator[Callable[..., TestClient]]:
    opened: list[TestClient] = []

    def factory(**overrides: object) -> TestClient:
        backend = overrides.pop("cluster", cluster)
        settings = ControlSettings(
            pod_name="control-test", chaos_enabled=True, chaos_token=TOKEN, log_json=False, **overrides
        )
        app = create_app(settings, cluster=backend, transport=httpx.MockTransport(pods))
        c = TestClient(app).__enter__()
        opened.append(c)
        return c

    yield factory
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-Chaos-Token": TOKEN}
