"""Control service entrypoint: `uvicorn --factory autoheal_control.main:create_app`."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse

from autoheal_common.app import create_service_app
from autoheal_common.health import HealthState
from autoheal_control.api import chaos_router, router
from autoheal_control.cluster import ClusterClient, build_cluster
from autoheal_control.config import ControlSettings
from autoheal_control.experiments import ExperimentRunner

STATIC = Path(__file__).parent / "static"


def create_app(
    settings: ControlSettings | None = None,
    cluster: ClusterClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    exit_fn: Callable[[int], None] = os._exit,
) -> FastAPI:
    settings = settings or ControlSettings()
    health = HealthState(settings.pod_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.cluster = cluster or build_cluster(
            settings.cluster_backend, settings.namespace, settings.platform_selector, settings.component_label
        )
        http = httpx.AsyncClient(transport=transport)
        app.state.runner = ExperimentRunner(settings, app.state.cluster, http)
        # Losing the API server degrades this service but must not restart it.
        health.add_readiness_check("kubernetes_api", app.state.cluster.ping, critical=False)
        health.mark_started()
        try:
            yield
        finally:
            health.shutting_down = True
            await http.aclose()

    app = create_service_app(
        settings=settings,
        health=health,
        lifespan=lifespan,
        title="AutoHeal Control API",
        description="Read-only cluster health, recovery timeline, and dev-only failure injection.",
        exit_fn=exit_fn,
    )
    app.include_router(router)

    if settings.chaos_active:
        token = settings.chaos_token.get_secret_value()

        def require_token(x_chaos_token: Annotated[str | None, Header()] = None) -> None:
            if not x_chaos_token or not secrets.compare_digest(x_chaos_token, token):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-Chaos-Token")

        app.include_router(chaos_router, dependencies=[Depends(require_token)])

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/v1/info", tags=["meta"])
    async def info() -> dict[str, object]:
        return {
            "namespace": settings.namespace,
            "cluster_backend": settings.cluster_backend,
            "chaos_enabled": settings.chaos_active,
            "environment": settings.environment,
        }

    return app
