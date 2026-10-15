from __future__ import annotations

from typing import Literal

from pydantic import Field

from autoheal_common.config import BaseServiceSettings


class TaskSettings(BaseServiceSettings):
    service_name: str = "tasks"

    redis_url: str = "redis://localhost:6379/0"

    # Concurrency per pod. Horizontal scale comes from the HPA adding pods.
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    compute_concurrency: int = Field(default=2, ge=1, le=32)
    # "process" keeps CPU-bound work off the event loop (and off the GIL) so
    # health probes stay responsive under load. "thread" is used by tests.
    executor: Literal["process", "thread"] = "process"

    # Reliable-queue tuning. A job whose lease is not renewed within
    # lease_ttl_seconds is assumed lost (its pod died) and is requeued.
    lease_ttl_seconds: float = Field(default=15, gt=0)
    reaper_interval_seconds: float = Field(default=5, gt=0)
    queue_poll_timeout_seconds: float = Field(default=1.0, gt=0)
    job_retention_seconds: int = 24 * 3600
    compute_timeout_seconds: float = 30
    shutdown_timeout_seconds: float = 20
