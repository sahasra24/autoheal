"""Job API contract, shared by the gateway (edge validation) and the task
service (re-validated - never trust the network, even inside the cluster)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrimeCountParams(_Strict):
    limit: int = Field(default=200_000, ge=2, le=5_000_000, description="Count primes below this number")


class HashChainParams(_Strict):
    iterations: int = Field(default=200_000, ge=1, le=5_000_000)
    seed: str = Field(default="autoheal", min_length=1, max_length=64)


class SleepParams(_Strict):
    seconds: float = Field(default=2.0, ge=0, le=60, description="Simulated I/O-bound work")


class FlakyParams(_Strict):
    failure_rate: float = Field(default=0.5, ge=0, le=1)
    seconds: float = Field(default=0.5, ge=0, le=10)


class PrimeCountJob(_Strict):
    type: Literal["prime_count"]
    params: PrimeCountParams = PrimeCountParams()
    max_attempts: int = Field(default=3, ge=1, le=5)


class HashChainJob(_Strict):
    type: Literal["hash_chain"]
    params: HashChainParams = HashChainParams()
    max_attempts: int = Field(default=3, ge=1, le=5)


class SleepJob(_Strict):
    type: Literal["sleep"]
    params: SleepParams = SleepParams()
    max_attempts: int = Field(default=3, ge=1, le=5)


class FlakyJob(_Strict):
    type: Literal["flaky"]
    params: FlakyParams = FlakyParams()
    max_attempts: int = Field(default=3, ge=1, le=5)


JobRequest = Annotated[
    PrimeCountJob | HashChainJob | SleepJob | FlakyJob,
    Field(discriminator="type"),
]
JOB_TYPES = ("prime_count", "hash_chain", "sleep", "flaky")


class Job(BaseModel):
    id: str
    type: str
    params: dict[str, Any]
    status: JobStatus
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    worker: str | None = None
    correlation_id: str | None = None


class JobList(BaseModel):
    items: list[Job]
    count: int


class ComputeRequest(_Strict):
    iterations: int = Field(default=50_000, ge=1, le=2_000_000)


class ComputeResponse(BaseModel):
    iterations: int
    digest: str
    duration_ms: float
    pod: str
