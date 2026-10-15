"""Application factory: wires the cross-cutting concerns every service shares."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware

from autoheal_common import __version__
from autoheal_common.chaos import ChaosMiddleware, ChaosState, build_chaos_router
from autoheal_common.config import BaseServiceSettings
from autoheal_common.health import HealthState, build_health_router
from autoheal_common.logging import configure_logging
from autoheal_common.metrics import SERVICE_INFO, build_metrics_router
from autoheal_common.middleware import CorrelationIdMiddleware, RequestMetricsMiddleware

log = logging.getLogger(__name__)


def create_service_app(
    *,
    settings: BaseServiceSettings,
    health: HealthState,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    title: str,
    description: str,
    exit_fn: Callable[[int], None] = os._exit,
    middleware: Sequence[Middleware] = (),
) -> FastAPI:
    configure_logging(settings.service_name, settings.log_level, settings.log_json)

    app = FastAPI(title=title, description=description, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.health = health

    app.include_router(build_health_router(health))
    app.include_router(build_metrics_router())

    if settings.chaos_active:
        chaos = ChaosState(health, exit_fn=exit_fn)
        app.state.chaos = chaos
        app.include_router(build_chaos_router(chaos, settings.chaos_token.get_secret_value()))
        app.add_middleware(ChaosMiddleware, chaos=chaos)
        log.warning("failure injection API is ENABLED - development use only")

    # Service-specific middleware (e.g. auth, rate limiting) runs inside the
    # observability layers so its rejections are still logged and counted.
    for mw in reversed(middleware):
        app.add_middleware(mw.cls, *mw.args, **mw.kwargs)

    # Last added = outermost: correlation ID must exist before metrics/logging run.
    app.add_middleware(RequestMetricsMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    SERVICE_INFO.labels(settings.service_name, __version__, settings.environment).set(1)
    return app
