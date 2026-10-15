from __future__ import annotations

from collections.abc import Iterator

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from autoheal_tasks.config import TaskSettings
from autoheal_tasks.main import create_app

CHAOS_TOKEN = "test-chaos-token"


@pytest.fixture
def settings() -> TaskSettings:
    return TaskSettings(
        pod_name="tasks-test-0",
        executor="thread",
        worker_concurrency=2,
        lease_ttl_seconds=0.6,
        reaper_interval_seconds=0.1,
        queue_poll_timeout_seconds=0.1,
        shutdown_timeout_seconds=1,
        chaos_enabled=True,
        chaos_token=CHAOS_TOKEN,
        log_json=False,
    )


@pytest.fixture
def chaos() -> dict[str, str]:
    return {"X-Chaos-Token": CHAOS_TOKEN}


@pytest.fixture
def exits() -> list[int]:
    return []


@pytest.fixture
def client(settings: TaskSettings, exits: list[int]) -> Iterator[TestClient]:
    app = create_app(settings, redis=FakeAsyncRedis(decode_responses=True), exit_fn=exits.append)
    with TestClient(app) as c:
        yield c
