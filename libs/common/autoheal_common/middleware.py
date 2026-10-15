"""Pure-ASGI middleware for correlation IDs and request metrics.

Pure ASGI (rather than BaseHTTPMiddleware) keeps contextvars intact for the
request's lifetime and adds no per-request task overhead.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autoheal_common.logging import correlation_id_var
from autoheal_common.metrics import HTTP_IN_PROGRESS, HTTP_LATENCY, HTTP_REQUESTS

CORRELATION_HEADER = "X-Correlation-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_QUIET_PATHS = ("/health", "/metrics")

access_log = logging.getLogger("autoheal.access")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class CorrelationIdMiddleware:
    """Accepts a caller-supplied correlation ID (if well formed) or mints one,
    exposes it to logs via a contextvar, and echoes it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope["headers"]:
            if name in (b"x-correlation-id", b"x-request-id"):
                incoming = value.decode("latin-1")
                break
        cid = incoming if incoming and _VALID_ID.match(incoming) else new_correlation_id()
        token = correlation_id_var.set(cid)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_HEADER] = cid
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            correlation_id_var.reset(token)


class RequestMetricsMiddleware:
    """Records RED metrics keyed by the *route template* (never the raw path,
    which would explode label cardinality) and writes one access-log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        HTTP_IN_PROGRESS.inc()
        try:
            await self.app(scope, receive, capture_status)
        finally:
            HTTP_IN_PROGRESS.dec()
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = scope["method"]
            HTTP_REQUESTS.labels(method, template, str(status)).inc()
            HTTP_LATENCY.labels(method, template).observe(elapsed)

            path = scope["path"]
            level = logging.DEBUG if path.startswith(_QUIET_PATHS) else logging.INFO
            access_log.log(
                level,
                "%s %s -> %s",
                method,
                path,
                status,
                extra={"route": template, "status": status, "duration_ms": round(elapsed * 1000, 2)},
            )
