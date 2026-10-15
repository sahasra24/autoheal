"""Task service entrypoint: `uvicorn --factory autoheal_tasks.main:create_app`."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from autoheal_common.app import create_service_app
from autoheal_common.health import HealthState
from autoheal_tasks.api import router
from autoheal_tasks.config import TaskSettings
from autoheal_tasks.store import JobStore
from autoheal_tasks.worker import WorkerPool, make_executor


def create_app(
    settings: TaskSettings | None = None,
    redis: Redis | None = None,
    exit_fn: Callable[[int], None] = os._exit,
) -> FastAPI:
    settings = settings or TaskSettings()
    health = HealthState(settings.pod_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=settings.queue_poll_timeout_seconds + 5,
            health_check_interval=15,
        )
        store = JobStore(client, lease_ttl=settings.lease_ttl_seconds, retention_seconds=settings.job_retention_seconds)
        job_executor = make_executor(settings.executor, settings.worker_concurrency)
        compute_executor = make_executor(settings.executor, settings.compute_concurrency)
        pool = WorkerPool(store, settings, job_executor, health)

        app.state.store = store
        app.state.pool = pool
        app.state.compute_executor = compute_executor
        # Redis is critical: without it this pod cannot accept or run jobs.
        # It is a readiness concern only - never liveness.
        health.add_readiness_check("redis", store.ping, critical=True)

        await pool.start()
        health.mark_started()
        try:
            yield
        finally:
            health.shutting_down = True
            await pool.stop(settings.shutdown_timeout_seconds)
            job_executor.shutdown(wait=False, cancel_futures=True)
            compute_executor.shutdown(wait=False, cancel_futures=True)
            if redis is None:
                await client.aclose()

    app = create_service_app(
        settings=settings,
        health=health,
        lifespan=lifespan,
        title="AutoHeal Task Service",
        description="Accepts background jobs, processes them on a reliable Redis queue, and exposes job metrics.",
        exit_fn=exit_fn,
    )
    app.include_router(router)
    return app
