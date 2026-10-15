"""Health model aligned with Kubernetes probe semantics.

* startup   - has the process finished initialising? (guards slow boots)
* liveness  - is the process itself functional? Never checks dependencies:
              restarting a pod because Redis is down would only add churn.
* readiness - should this pod receive traffic right now? Checks critical
              dependencies and goes false during shutdown so the pod drains.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

log = logging.getLogger(__name__)

HealthCheck = Callable[[], Awaitable[None]]  # raises on failure


@dataclass
class _Check:
    fn: HealthCheck
    critical: bool


class CheckResult(BaseModel):
    ok: bool
    critical: bool
    latency_ms: float
    error: str | None = None


class HealthReport(BaseModel):
    status: str
    pod: str
    checks: dict[str, CheckResult] = {}
    reason: str | None = None


class HealthState:
    def __init__(self, pod_name: str, check_timeout: float = 2.0) -> None:
        self.pod_name = pod_name
        self.check_timeout = check_timeout
        self.started = False
        self.shutting_down = False
        self.unhealthy_reason: str | None = None
        self.unready_until: float = 0.0
        self._checks: dict[str, _Check] = {}

    # --- mutation -------------------------------------------------------
    def add_readiness_check(self, name: str, fn: HealthCheck, *, critical: bool = True) -> None:
        self._checks[name] = _Check(fn, critical)

    def mark_started(self) -> None:
        self.started = True

    def mark_unhealthy(self, reason: str) -> None:
        log.error("liveness forced to fail", extra={"reason": reason})
        self.unhealthy_reason = reason

    def force_unready(self, seconds: float) -> None:
        self.unready_until = time.monotonic() + seconds

    def reset(self) -> None:
        self.unhealthy_reason = None
        self.unready_until = 0.0

    # --- evaluation -----------------------------------------------------
    @property
    def live(self) -> bool:
        return self.unhealthy_reason is None

    async def _run_check(self, name: str, check: _Check) -> CheckResult:
        start = time.perf_counter()
        try:
            await asyncio.wait_for(check.fn(), timeout=self.check_timeout)
            error = None
        except Exception as exc:  # noqa: BLE001 - any failure means "not ok"
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return CheckResult(
            ok=error is None,
            critical=check.critical,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            error=error,
        )

    async def readiness(self) -> HealthReport:
        names = list(self._checks)
        results = await asyncio.gather(*(self._run_check(n, self._checks[n]) for n in names))
        checks = dict(zip(names, results, strict=True))

        reason = None
        if not self.started:
            reason = "starting"
        elif self.shutting_down:
            reason = "shutting down"
        elif time.monotonic() < self.unready_until:
            reason = "forced unready (failure injection)"
        elif failed := [n for n, r in checks.items() if r.critical and not r.ok]:
            reason = f"critical dependency down: {', '.join(failed)}"

        degraded = any(not r.ok for r in checks.values())
        state = "unready" if reason else ("degraded" if degraded else "ready")
        return HealthReport(status=state, pod=self.pod_name, checks=checks, reason=reason)


def build_health_router(state: HealthState) -> APIRouter:
    router = APIRouter(prefix="/health", tags=["health"])

    @router.get("/live", response_model=HealthReport)
    async def live(response: Response) -> HealthReport:
        if not state.live:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthReport(status="unhealthy", pod=state.pod_name, reason=state.unhealthy_reason)
        return HealthReport(status="alive", pod=state.pod_name)

    @router.get("/ready", response_model=HealthReport)
    async def ready(response: Response) -> HealthReport:
        report = await state.readiness()
        if report.status == "unready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return report

    @router.get("/startup", response_model=HealthReport)
    async def startup(response: Response) -> HealthReport:
        if not state.started:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthReport(status="starting", pod=state.pod_name)
        return HealthReport(status="started", pod=state.pod_name)

    return router
