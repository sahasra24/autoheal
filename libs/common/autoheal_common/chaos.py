"""Development-only failure injection.

Each injection maps to a self-healing scenario:

    crash      -> process exits; kubelet restarts the container (restartPolicy)
    unhealthy  -> liveness probe fails; kubelet kills and restarts the container
    unready    -> readiness probe fails; pod is removed from Service endpoints
    cpu        -> CPU saturation; HorizontalPodAutoscaler adds replicas
    memory     -> memory grows past the limit; kernel OOM-kills, kubelet restarts
    latency    -> slow responses; latency alerts fire
    errors     -> random 5xx; error-rate alerts fire

The router is only mounted when `chaos_active` is true (never in production)
and every call requires the shared X-Chaos-Token secret.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import random
import secrets
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from autoheal_common.health import HealthState
from autoheal_common.metrics import CHAOS_INJECTIONS

log = logging.getLogger(__name__)

_MB = 1024 * 1024


def burn_cpu(seconds: float) -> None:
    """Spin one core. Module-level so it can run in a spawned process."""
    deadline = time.monotonic() + seconds
    x = 0
    while time.monotonic() < deadline:
        x = (x * 31 + 7) % 1_000_003


class ChaosState:
    def __init__(self, health: HealthState, exit_fn: Callable[[int], None] = os._exit) -> None:
        self.health = health
        self.exit_fn = exit_fn
        self.latency_ms = 0
        self.error_rate = 0.0
        self._memory: list[bytes] = []
        self._burners: list[multiprocessing.process.BaseProcess] = []
        self.history: deque[dict[str, object]] = deque(maxlen=50)

    @property
    def memory_mb(self) -> int:
        return sum(len(b) for b in self._memory) // _MB

    @property
    def active_cpu_burners(self) -> int:
        self._burners = [p for p in self._burners if p.is_alive()]
        return len(self._burners)

    def record(self, kind: str, **detail: object) -> dict[str, object]:
        CHAOS_INJECTIONS.labels(kind).inc()
        entry = {"kind": kind, "at": datetime.now(UTC).isoformat(), **detail}
        self.history.append(entry)
        log.warning("failure injected", extra={"chaos": entry})
        return entry

    def start_cpu_burn(self, seconds: float, workers: int) -> None:
        ctx = multiprocessing.get_context("spawn")
        for _ in range(workers):
            proc = ctx.Process(target=burn_cpu, args=(seconds,), daemon=True)
            proc.start()
            self._burners.append(proc)

    def allocate_memory(self, mb: int) -> None:
        # bytes multiplication writes every page, so RSS really grows
        # (a zeroed bytearray could stay lazily unbacked).
        for _ in range(mb):
            self._memory.append(b"\x01" * _MB)

    def reset(self) -> None:
        self.latency_ms = 0
        self.error_rate = 0.0
        self._memory.clear()
        for proc in self._burners:
            if proc.is_alive():
                proc.terminate()
        self._burners.clear()
        self.health.reset()

    def snapshot(self) -> dict[str, object]:
        return {
            "live": self.health.live,
            "unhealthy_reason": self.health.unhealthy_reason,
            "unready_for_seconds": max(0.0, round(self.health.unready_until - time.monotonic(), 1)),
            "latency_ms": self.latency_ms,
            "error_rate": self.error_rate,
            "memory_mb": self.memory_mb,
            "cpu_burners": self.active_cpu_burners,
            "history": list(self.history),
        }


class CrashRequest(BaseModel):
    exit_code: int = Field(default=1, ge=1, le=255)
    delay_seconds: float = Field(default=0.5, ge=0, le=10, description="Lets the HTTP response flush first")


class UnreadyRequest(BaseModel):
    seconds: float = Field(default=60, ge=1, le=900)


class CpuRequest(BaseModel):
    seconds: float = Field(default=120, ge=1, le=900)
    workers: int = Field(default=2, ge=1, le=8)


class MemoryRequest(BaseModel):
    mb: int = Field(default=256, ge=1, le=4096)


class LatencyRequest(BaseModel):
    ms: int = Field(ge=0, le=30_000)


class ErrorRateRequest(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)


def build_chaos_router(chaos: ChaosState, token: str) -> APIRouter:
    def require_token(x_chaos_token: Annotated[str | None, Header()] = None) -> None:
        if not x_chaos_token or not secrets.compare_digest(x_chaos_token, token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-Chaos-Token")

    router = APIRouter(prefix="/chaos", tags=["chaos (dev only)"], dependencies=[Depends(require_token)])

    @router.get("")
    async def state() -> dict[str, object]:
        return chaos.snapshot()

    @router.post("/crash", status_code=status.HTTP_202_ACCEPTED)
    async def crash(req: CrashRequest) -> dict[str, object]:
        entry = chaos.record("crash", exit_code=req.exit_code)
        asyncio.get_running_loop().call_later(req.delay_seconds, chaos.exit_fn, req.exit_code)
        return entry

    @router.post("/unhealthy", status_code=status.HTTP_202_ACCEPTED)
    async def unhealthy() -> dict[str, object]:
        chaos.health.mark_unhealthy("failure injection: simulated deadlock")
        return chaos.record("unhealthy")

    @router.post("/unready", status_code=status.HTTP_202_ACCEPTED)
    async def unready(req: UnreadyRequest) -> dict[str, object]:
        chaos.health.force_unready(req.seconds)
        return chaos.record("unready", seconds=req.seconds)

    @router.post("/cpu", status_code=status.HTTP_202_ACCEPTED)
    async def cpu(req: CpuRequest) -> dict[str, object]:
        chaos.start_cpu_burn(req.seconds, req.workers)
        return chaos.record("cpu", seconds=req.seconds, workers=req.workers)

    @router.post("/memory", status_code=status.HTTP_202_ACCEPTED)
    async def memory(req: MemoryRequest) -> dict[str, object]:
        await asyncio.to_thread(chaos.allocate_memory, req.mb)
        return chaos.record("memory", added_mb=req.mb, total_mb=chaos.memory_mb)

    @router.post("/latency")
    async def latency(req: LatencyRequest) -> dict[str, object]:
        chaos.latency_ms = req.ms
        return chaos.record("latency", ms=req.ms)

    @router.post("/errors")
    async def errors(req: ErrorRateRequest) -> dict[str, object]:
        chaos.error_rate = req.rate
        return chaos.record("errors", rate=req.rate)

    @router.post("/reset")
    async def reset() -> dict[str, object]:
        chaos.reset()
        chaos.record("reset")
        return chaos.snapshot()

    return router


class ChaosMiddleware:
    """Applies injected latency / error rate to business routes (/api/*) only,
    so health probes and metrics keep reporting truthfully."""

    def __init__(self, app: ASGIApp, chaos: ChaosState) -> None:
        self.app = app
        self.chaos = chaos

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api"):
            await self.app(scope, receive, send)
            return
        if self.chaos.latency_ms:
            await asyncio.sleep(self.chaos.latency_ms / 1000)
        if self.chaos.error_rate and random.random() < self.chaos.error_rate:
            body = json.dumps({"detail": "injected failure"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
