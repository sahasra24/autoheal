"""Resilient HTTP client for one backend service."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import Response

from autoheal_common.logging import correlation_id_var
from autoheal_common.middleware import CORRELATION_HEADER
from autoheal_gateway.circuit import CircuitBreaker
from autoheal_gateway.metrics import UPSTREAM_LATENCY, UPSTREAM_REQUESTS

log = logging.getLogger(__name__)

# Upstream headers worth passing back to the client.
_PASSTHROUGH = ("content-type", "location", "retry-after")


class UpstreamError(Exception):
    def __init__(self, status_code: int, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


class Upstream:
    def __init__(self, name: str, client: httpx.AsyncClient, breaker: CircuitBreaker) -> None:
        self.name = name
        self.client = client
        self.breaker = breaker

    async def forward(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> Response:
        if not self.breaker.allow():
            UPSTREAM_REQUESTS.labels(self.name, "circuit_open").inc()
            raise UpstreamError(503, f"{self.name} service unavailable (circuit open)", self.breaker.retry_after())

        headers = {CORRELATION_HEADER: correlation_id_var.get() or ""}
        if client_ip:
            headers["X-Forwarded-For"] = client_ip
        params = {k: v for k, v in (params or {}).items() if v is not None}

        # Idempotent reads get one retry on connection failure: during a
        # rolling restart the first attempt can hit a pod that just went away.
        attempts = 2 if method == "GET" else 1
        start = time.perf_counter()
        for attempt in range(1, attempts + 1):
            try:
                upstream = await self.client.request(method, path, json=json, params=params, headers=headers)
                break
            except httpx.ConnectError as exc:
                if attempt < attempts:
                    continue
                self._failed("connect_error", exc)
                raise UpstreamError(503, f"{self.name} service unavailable") from exc
            except httpx.TimeoutException as exc:
                self._failed("timeout", exc)
                raise UpstreamError(504, f"{self.name} service timed out") from exc
            except httpx.HTTPError as exc:
                self._failed("error", exc)
                raise UpstreamError(502, f"bad response from {self.name} service") from exc
        UPSTREAM_LATENCY.labels(self.name).observe(time.perf_counter() - start)

        if upstream.status_code >= 500:
            self.breaker.record_failure()
            UPSTREAM_REQUESTS.labels(self.name, "server_error").inc()
        else:
            self.breaker.record_success()
            UPSTREAM_REQUESTS.labels(self.name, "ok").inc()

        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in _PASSTHROUGH}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)

    def _failed(self, outcome: str, exc: Exception) -> None:
        self.breaker.record_failure()
        UPSTREAM_REQUESTS.labels(self.name, outcome).inc()
        log.warning("upstream call failed", extra={"upstream": self.name, "outcome": outcome, "error": repr(exc)})
