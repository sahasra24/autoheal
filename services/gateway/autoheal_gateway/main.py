"""Gateway entrypoint: `uvicorn --factory autoheal_gateway.main:create_app`."""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.middleware import Middleware

from autoheal_common import __version__
from autoheal_common.app import create_service_app
from autoheal_common.health import HealthState
from autoheal_gateway.access import AccessControlMiddleware
from autoheal_gateway.circuit import CircuitBreaker
from autoheal_gateway.config import GatewaySettings
from autoheal_gateway.rate_limit import SlidingWindowRateLimiter
from autoheal_gateway.routes import cluster, jobs
from autoheal_gateway.upstream import Upstream, UpstreamError


def create_app(
    settings: GatewaySettings | None = None,
    redis: Redis | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    exit_fn: Callable[[int], None] = os._exit,
) -> FastAPI:
    settings = settings or GatewaySettings()
    health = HealthState(settings.pod_name)

    # redis-py connects lazily, so constructing the client here is side-effect free.
    redis_client = redis or Redis.from_url(
        settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1
    )
    limiter = (
        SlidingWindowRateLimiter(
            redis_client,
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            fail_open=settings.rate_limit_fail_open,
        )
        if settings.rate_limit_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timeout = httpx.Timeout(settings.upstream_timeout_seconds, connect=settings.upstream_connect_timeout_seconds)
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
        clients = {
            name: httpx.AsyncClient(base_url=url, timeout=timeout, limits=limits, transport=transport)
            for name, url in (("tasks", settings.tasks_url), ("control", settings.control_url))
        }
        app.state.upstreams = {
            name: Upstream(
                name,
                client,
                CircuitBreaker(
                    name,
                    failure_threshold=settings.circuit_failure_threshold,
                    reset_seconds=settings.circuit_reset_seconds,
                ),
            )
            for name, client in clients.items()
        }

        async def redis_check() -> None:
            await redis_client.ping()

        # Non-critical: the gateway keeps serving (rate limiter fails open)
        # and reports "degraded" rather than dropping out of the Service.
        health.add_readiness_check("redis", redis_check, critical=not settings.rate_limit_fail_open)
        health.mark_started()
        try:
            yield
        finally:
            health.shutting_down = True
            for client in clients.values():
                await client.aclose()
            if redis is None:
                await redis_client.aclose()

    app = create_service_app(
        settings=settings,
        health=health,
        lifespan=lifespan,
        title="AutoHeal API Gateway",
        description="Single entry point: validation, auth, distributed rate limiting, and resilient routing.",
        exit_fn=exit_fn,
        middleware=[Middleware(AccessControlMiddleware, settings=settings, limiter=limiter)],
    )

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after)))} if exc.retry_after is not None else None
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, object]:
        return {
            "service": "autoheal-gateway",
            "version": __version__,
            "pod": settings.pod_name,
            "docs": "/docs",
            "endpoints": ["/api/v1/jobs", "/api/v1/compute", "/api/v1/queue", "/api/v1/cluster/{resource}"],
        }

    app.include_router(jobs.router)
    app.include_router(cluster.router)
    return app
