from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from autoheal_gateway.config import GatewaySettings
from autoheal_gateway.main import create_app

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeBackends:
    """httpx MockTransport standing in for the tasks and control services."""

    calls: list[httpx.Request] = field(default_factory=list)
    handler: Handler | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.handler:
            return self.handler(request)
        if request.url.host == "tasks" and request.url.path == "/api/v1/jobs" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                202,
                json={"id": "11111111-1111-1111-1111-111111111111", "type": body["type"], "status": "queued"},
                headers={"Location": "/api/v1/jobs/11111111-1111-1111-1111-111111111111"},
            )
        if request.url.host == "control":
            return httpx.Response(200, json={"resource": request.url.path})
        return httpx.Response(200, json={"path": request.url.path, "query": dict(request.url.params)})


@pytest.fixture
def backends() -> FakeBackends:
    return FakeBackends()


@pytest.fixture
def settings() -> GatewaySettings:
    return GatewaySettings(
        pod_name="gateway-test-0",
        tasks_url="http://tasks",
        control_url="http://control",
        rate_limit_requests=5,
        rate_limit_window_seconds=60,
        circuit_failure_threshold=2,
        circuit_reset_seconds=30,
        log_json=False,
    )


@pytest.fixture
def make_client(settings: GatewaySettings, backends: FakeBackends) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(**overrides: object) -> TestClient:
        s = settings.model_copy(update=overrides)
        app = create_app(s, redis=FakeAsyncRedis(decode_responses=True), transport=httpx.MockTransport(backends))
        client = TestClient(app).__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()
