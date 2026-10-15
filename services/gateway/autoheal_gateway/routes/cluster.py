"""Read-only cluster views, proxied from the control service.

Failure injection is deliberately NOT exposed through the gateway: it is only
reachable on the control service's internal port (kubectl port-forward).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from autoheal_gateway.upstream import Upstream

router = APIRouter(prefix="/api/v1/cluster", tags=["cluster"])


class Resource(StrEnum):
    overview = "overview"
    deployments = "deployments"
    pods = "pods"
    autoscalers = "autoscalers"
    events = "events"
    recovery = "recovery"


@router.get("/{resource}")
async def cluster_view(
    resource: Resource,
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> Response:
    control: Upstream = request.app.state.upstreams["control"]
    return await control.forward("GET", f"/api/v1/{resource.value}", params={"limit": limit})
