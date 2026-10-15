from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from redis.exceptions import RedisError

from autoheal_common.logging import correlation_id_var
from autoheal_common.schemas import ComputeRequest, ComputeResponse, Job, JobList, JobRequest, JobStatus
from autoheal_tasks.metrics import COMPUTE_DURATION, JOBS_SUBMITTED
from autoheal_tasks.store import JobStore
from autoheal_tasks.workloads import hash_chain

router = APIRouter(prefix="/api/v1", tags=["jobs"])

JobId = Annotated[str, Path(pattern=r"^[0-9a-f-]{36}$", description="Job UUID")]


def _store(request: Request) -> JobStore:
    return request.app.state.store


def _unavailable(exc: RedisError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"job store unavailable: {type(exc).__name__}")


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=Job)
async def submit_job(body: JobRequest, request: Request, response: Response) -> Job:
    try:
        job = await _store(request).create(body, correlation_id_var.get())
    except RedisError as exc:
        raise _unavailable(exc) from exc
    JOBS_SUBMITTED.labels(job.type).inc()
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return job


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> JobList:
    try:
        jobs = await _store(request).list(limit, status_filter)
    except RedisError as exc:
        raise _unavailable(exc) from exc
    return JobList(items=jobs, count=len(jobs))


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: JobId, request: Request) -> Job:
    try:
        job = await _store(request).get(job_id)
    except RedisError as exc:
        raise _unavailable(exc) from exc
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.get("/queue")
async def queue_depth(request: Request) -> dict[str, int]:
    try:
        return await _store(request).depth()
    except RedisError as exc:
        raise _unavailable(exc) from exc


@router.post("/compute", response_model=ComputeResponse)
async def compute(body: ComputeRequest, request: Request) -> ComputeResponse:
    """Synchronous CPU-bound request; the load-test lever that drives the HPA."""
    settings = request.app.state.settings
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(request.app.state.compute_executor, hash_chain, body.iterations, "compute"),
            timeout=settings.compute_timeout_seconds,
        )
    except TimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "compute timed out") from exc
    elapsed = time.perf_counter() - start
    COMPUTE_DURATION.observe(elapsed)
    return ComputeResponse(
        iterations=body.iterations,
        digest=result["digest"],
        duration_ms=round(elapsed * 1000, 2),
        pod=settings.pod_name,
    )
