"""Prometheus metrics shared by all services and the /metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "autoheal_http_requests_total",
    "HTTP requests handled, by route template and status code.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "autoheal_http_request_duration_seconds",
    "HTTP request latency by route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
HTTP_IN_PROGRESS = Gauge(
    "autoheal_http_requests_in_progress",
    "HTTP requests currently being handled.",
)
SERVICE_INFO = Gauge(
    "autoheal_service_info",
    "Static service metadata (always 1).",
    ["service", "version", "environment"],
)
CHAOS_INJECTIONS = Counter(
    "autoheal_chaos_injections_total",
    "Failure injections applied to this instance.",
    ["kind"],
)


def build_metrics_router() -> APIRouter:
    router = APIRouter(tags=["observability"])

    @router.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return router
