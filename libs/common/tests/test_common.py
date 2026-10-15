from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from autoheal_common.app import create_service_app
from autoheal_common.config import BaseServiceSettings
from autoheal_common.health import HealthState
from autoheal_common.logging import JsonFormatter, correlation_id_var


def _app(health: HealthState, **settings: object) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        health.mark_started()
        yield

    app = create_service_app(
        settings=BaseServiceSettings(log_json=False, **settings),
        health=health,
        lifespan=lifespan,
        title="t",
        description="t",
        exit_fn=lambda code: None,
    )

    @app.get("/api/echo/{item}")
    async def echo(item: str) -> dict[str, str | None]:
        return {"item": item, "cid": correlation_id_var.get()}

    return app


def test_correlation_id_propagates_to_handler_and_response() -> None:
    with TestClient(_app(HealthState("p"))) as c:
        resp = c.get("/api/echo/x", headers={"X-Request-ID": "upstream-id-42"})
        assert resp.json()["cid"] == "upstream-id-42"
        assert resp.headers["X-Correlation-ID"] == "upstream-id-42"


def test_metrics_use_route_template_not_raw_path() -> None:
    with TestClient(_app(HealthState("p"))) as c:
        for i in range(3):
            c.get(f"/api/echo/item-{i}")
        text = c.get("/metrics").text
        assert 'route="/api/echo/{item}"' in text
        assert "item-1" not in text


def test_startup_and_readiness_wait_for_lifespan() -> None:
    health = HealthState("p")
    app = _app(health)
    with TestClient(app) as c:
        assert c.get("/health/startup").status_code == 200
    # before startup completes
    fresh = HealthState("p")
    assert asyncio.run(fresh.readiness()).reason == "starting"


def test_critical_check_fails_readiness_noncritical_degrades() -> None:
    async def broken() -> None:
        raise ConnectionError("down")

    health = HealthState("p")
    health.add_readiness_check("cache", broken, critical=False)
    with TestClient(_app(health)) as c:
        body = c.get("/health/ready").json()
        assert body["status"] == "degraded" and body["checks"]["cache"]["ok"] is False

    health = HealthState("p")
    health.add_readiness_check("db", broken, critical=True)
    with TestClient(_app(health)) as c:
        resp = c.get("/health/ready")
        assert resp.status_code == 503 and "db" in resp.json()["reason"]
        assert c.get("/health/live").status_code == 200  # dependencies never affect liveness


def test_slow_check_times_out() -> None:
    async def hang() -> None:
        await asyncio.sleep(10)

    health = HealthState("p", check_timeout=0.05)
    health.add_readiness_check("slow", hang)
    health.mark_started()
    report = asyncio.run(health.readiness())
    assert report.status == "unready" and "TimeoutError" in report.checks["slow"].error


def test_chaos_disabled_by_default_and_in_production() -> None:
    assert not BaseServiceSettings().chaos_active
    assert not BaseServiceSettings(chaos_enabled=True, chaos_token="x", environment="production").chaos_active
    assert not BaseServiceSettings(chaos_enabled=True).chaos_active  # token required
    assert BaseServiceSettings(chaos_enabled=True, chaos_token="x").chaos_active
    with TestClient(_app(HealthState("p"))) as c:
        assert c.post("/chaos/crash").status_code == 404


def test_json_log_includes_correlation_id_and_extras() -> None:
    token = correlation_id_var.set("cid-for-log-1")
    try:
        record = logging.LogRecord("x", logging.INFO, "f", 1, "hello %s", ("world",), None)
        record.job_id = "j1"
        out = json.loads(JsonFormatter("svc").format(record))
    finally:
        correlation_id_var.reset(token)
    assert out["msg"] == "hello world" and out["correlation_id"] == "cid-for-log-1"
    assert out["job_id"] == "j1" and out["service"] == "svc"
