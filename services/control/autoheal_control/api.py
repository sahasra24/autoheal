from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from prometheus_client import Counter

from autoheal_control.cluster import RECOVERY_CATEGORIES, ClusterClient, ClusterUnavailable
from autoheal_control.experiments import CATALOG, ExperimentRunner
from autoheal_control.models import (
    AutoscalerStatus,
    ClusterEvent,
    DeploymentStatus,
    EventCategory,
    Experiment,
    ExperimentRequest,
    Overview,
    PodStatus,
)

K8S_ERRORS = Counter("autoheal_k8s_api_errors_total", "Failed Kubernetes API calls from the control service.")

router = APIRouter(prefix="/api/v1", tags=["cluster (read-only)"])
chaos_router = APIRouter(prefix="/api/v1/chaos", tags=["failure injection (dev only)"])

Limit = Annotated[int | None, Query(ge=1, le=500)]


def _cluster(request: Request) -> ClusterClient:
    return request.app.state.cluster


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except ClusterUnavailable as exc:
        K8S_ERRORS.inc()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/deployments", response_model=list[DeploymentStatus])
async def deployments(request: Request) -> list[DeploymentStatus]:
    return await _guard(_cluster(request).list_deployments())


@router.get("/pods", response_model=list[PodStatus])
async def pods(request: Request, component: str | None = None) -> list[PodStatus]:
    return await _guard(_cluster(request).list_pods(component))


@router.get("/autoscalers", response_model=list[AutoscalerStatus])
async def autoscalers(request: Request) -> list[AutoscalerStatus]:
    return await _guard(_cluster(request).list_autoscalers())


@router.get("/events", response_model=list[ClusterEvent])
async def events(request: Request, limit: Limit = None) -> list[ClusterEvent]:
    return await _guard(_cluster(request).list_events(limit or request.app.state.settings.events_limit_default))


@router.get("/recovery", response_model=list[ClusterEvent])
async def recovery(request: Request, limit: Limit = None) -> list[ClusterEvent]:
    """Self-healing timeline: recovery-relevant events, plus each pod's last
    container termination (which outlives the ~1h event TTL)."""
    limit = limit or request.app.state.settings.events_limit_default
    cluster = _cluster(request)
    evts, pod_list = await _guard(asyncio.gather(cluster.list_events(500), cluster.list_pods()))
    timeline = [e for e in evts if e.category in RECOVERY_CATEGORIES]
    seen = {(e.object_name, e.reason) for e in timeline}
    for pod in pod_list:
        term = pod.last_termination
        if term and term.reason and (pod.name, term.reason) not in seen:
            timeline.append(
                ClusterEvent(
                    timestamp=term.finished_at,
                    type="Warning",
                    reason=term.reason,
                    category=EventCategory.oom if term.reason == "OOMKilled" else EventCategory.restart,
                    object_kind="Pod",
                    object_name=pod.name,
                    message=f"container terminated ({term.reason}, exit {term.exit_code}); restarted by kubelet",
                )
            )
    epoch = datetime.min.replace(tzinfo=UTC)
    timeline.sort(key=lambda e: e.timestamp or epoch, reverse=True)
    return timeline[:limit]


@router.get("/overview", response_model=Overview)
async def overview(request: Request) -> Overview:
    cluster = _cluster(request)
    deps, pod_list, hpas, evts = await _guard(
        asyncio.gather(
            cluster.list_deployments(), cluster.list_pods(), cluster.list_autoscalers(), cluster.list_events(200)
        )
    )
    return Overview(
        namespace=cluster.namespace,
        healthy=bool(deps) and all(d.healthy for d in deps),
        deployments=deps,
        pods_total=len(pod_list),
        pods_ready=sum(p.ready for p in pod_list),
        total_restarts=sum(p.restarts for p in pod_list),
        autoscalers=hpas,
        recent_recovery_events=sum(e.category in RECOVERY_CATEGORIES for e in evts),
    )


# --- failure injection ---------------------------------------------------


def _runner(request: Request) -> ExperimentRunner:
    return request.app.state.runner


@chaos_router.get("/scenarios")
async def scenarios(request: Request) -> list[dict[str, Any]]:
    settings = request.app.state.settings
    return [
        {
            "scenario": s.value,
            "expected_recovery": expected,
            "params_schema": model.model_json_schema(),
            "allowed_targets": sorted(settings.delete_targets if endpoint is None else settings.http_targets),
        }
        for s, (endpoint, model, expected) in CATALOG.items()
    ]


@chaos_router.post("/experiments", status_code=status.HTTP_201_CREATED, response_model=Experiment)
async def run_experiment(body: ExperimentRequest, request: Request) -> Experiment:
    return await _runner(request).run(body)


@chaos_router.get("/experiments", response_model=list[Experiment])
async def list_experiments(request: Request) -> list[Experiment]:
    return list(_runner(request).history)
