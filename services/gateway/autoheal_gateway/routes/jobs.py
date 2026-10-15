"""Job routes. Payloads are validated here, at the edge, before any backend
call - malformed requests never consume backend capacity."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from autoheal_common.schemas import ComputeRequest, ComputeResponse, Job, JobList, JobRequest, JobStatus
from autoheal_gateway.access import client_ip
from autoheal_gateway.upstream import Upstream

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _tasks(request: Request) -> Upstream:
    return request.app.state.upstreams["tasks"]


def _ip(request: Request) -> str:
    return client_ip(request.scope, request.app.state.settings.trust_forwarded_for)


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=Job)
async def submit_job(body: JobRequest, request: Request) -> Response:
    """Submit a background job (prime_count, hash_chain, sleep, flaky)."""
    return await _tasks(request).forward(
        "POST", "/api/v1/jobs", json=body.model_dump(mode="json"), client_ip=_ip(request)
    )


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> Response:
    params = {"limit": limit, "status": status_filter.value if status_filter else None}
    return await _tasks(request).forward("GET", "/api/v1/jobs", params=params, client_ip=_ip(request))


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: UUID, request: Request) -> Response:
    return await _tasks(request).forward("GET", f"/api/v1/jobs/{job_id}", client_ip=_ip(request))


@router.get("/queue")
async def queue_depth(request: Request) -> Response:
    return await _tasks(request).forward("GET", "/api/v1/queue", client_ip=_ip(request))


@router.post("/compute", response_model=ComputeResponse)
async def compute(body: ComputeRequest, request: Request) -> Response:
    """Synchronous CPU-bound work; used by load tests to trigger autoscaling."""
    return await _tasks(request).forward("POST", "/api/v1/compute", json=body.model_dump(), client_ip=_ip(request))
