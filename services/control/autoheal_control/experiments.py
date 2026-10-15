"""Failure-injection orchestration (development only).

Targets are restricted to allow-listed platform components; pods are
resolved through the Kubernetes API and the injection is sent straight to
the chosen pod's IP, so the experiment hits exactly one replica rather than
whichever one the Service load-balances to.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import HTTPException, status
from prometheus_client import Counter
from pydantic import BaseModel, ValidationError

from autoheal_common.chaos import (
    CpuRequest,
    CrashRequest,
    ErrorRateRequest,
    LatencyRequest,
    MemoryRequest,
    UnreadyRequest,
)
from autoheal_control.cluster import ClusterClient, ClusterUnavailable
from autoheal_control.config import ControlSettings
from autoheal_control.models import Experiment, ExperimentRequest, PodStatus, Scenario

log = logging.getLogger(__name__)

EXPERIMENTS = Counter("autoheal_chaos_experiments_total", "Failure-injection experiments.", ["scenario", "outcome"])


class _Empty(BaseModel):
    pass


# scenario -> (target endpoint, params model, what Kubernetes should do about it)
CATALOG: dict[Scenario, tuple[str | None, type[BaseModel], str]] = {
    Scenario.crash: (
        "crash",
        CrashRequest,
        "Process exits -> kubelet restarts the container (restartPolicy: Always); restart count +1.",
    ),
    Scenario.unhealthy: (
        "unhealthy",
        _Empty,
        "Liveness probe fails 3x -> kubelet kills and restarts the container.",
    ),
    Scenario.unready: (
        "unready",
        UnreadyRequest,
        "Readiness probe fails -> pod removed from Service endpoints, no restart; rejoins when ready.",
    ),
    Scenario.cpu_stress: (
        "cpu",
        CpuRequest,
        "CPU above HPA target -> HorizontalPodAutoscaler adds replicas; scales back after stabilisation window.",
    ),
    Scenario.memory_leak: (
        "memory",
        MemoryRequest,
        "Memory exceeds container limit -> kernel OOM-kills it (OOMKilled) -> kubelet restarts it.",
    ),
    Scenario.latency: ("latency", LatencyRequest, "Responses slow down -> p95 latency alert fires."),
    Scenario.error_rate: (
        "errors",
        ErrorRateRequest,
        "Random 5xx -> error-rate alert fires; gateway breaker may open.",
    ),
    Scenario.reset: ("reset", _Empty, "Clears all injected faults on the pod."),
    Scenario.pod_delete: (
        None,
        _Empty,
        "Pod deleted -> ReplicaSet controller schedules a replacement to restore desired replicas.",
    ),
}


class ExperimentRunner:
    def __init__(self, settings: ControlSettings, cluster: ClusterClient, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.cluster = cluster
        self.http = http
        self.history: deque[Experiment] = deque(maxlen=100)

    async def run(self, req: ExperimentRequest) -> Experiment:
        endpoint, params_model, expected = CATALOG[req.scenario]
        try:
            params = params_model.model_validate(req.params).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_url=False)) from exc

        allowed = self.settings.delete_targets if endpoint is None else self.settings.http_targets
        if req.target not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"target '{req.target}' is not allow-listed for {req.scenario}; allowed: {sorted(allowed)}",
            )

        pod: PodStatus | None = None
        base_url: str | None = None
        direct = self.settings.direct_targets.get(req.target)
        if direct and req.pod is None and endpoint is not None:
            base_url = direct  # compose mode: no pod resolution
        else:
            pod = await self._pick_pod(req.target, req.pod)
            base_url = f"http://{pod.pod_ip}:{self.settings.chaos_target_port}"

        experiment = Experiment(
            id=uuid.uuid4().hex[:12],
            scenario=req.scenario,
            target=req.target,
            pod=pod.name if pod else base_url,
            params=params,
            status="injected",
            detail="",
            expected_recovery=expected,
            created_at=datetime.now(UTC),
            restarts_before=pod.restarts if pod else None,
        )
        try:
            if endpoint is None:
                await self.cluster.delete_pod(pod.name)  # type: ignore[union-attr]
                experiment.detail = f"deleted pod {pod.name}"  # type: ignore[union-attr]
            else:
                experiment.detail = await self._inject(f"{base_url}/chaos/{endpoint}", params)
        except (ClusterUnavailable, httpx.HTTPError, RuntimeError) as exc:
            experiment.status = "failed"
            experiment.detail = f"{type(exc).__name__}: {exc}"

        EXPERIMENTS.labels(req.scenario.value, experiment.status).inc()
        log.warning("failure-injection experiment", extra={"experiment": experiment.model_dump(mode="json")})
        self.history.appendleft(experiment)
        return experiment

    async def _pick_pod(self, component: str, name: str | None) -> PodStatus:
        try:
            pods = await self.cluster.list_pods(component)
        except ClusterUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        if name:
            match = next((p for p in pods if p.name == name), None)
            if match is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"pod '{name}' not found in component '{component}'")
            return match
        candidates = [p for p in pods if p.ready and not p.terminating and p.pod_ip]
        if not candidates:
            raise HTTPException(status.HTTP_409_CONFLICT, f"no ready pods for component '{component}'")
        return random.choice(candidates)

    async def _inject(self, url: str, params: dict[str, Any]) -> str:
        resp = await self.http.post(
            url,
            json=params,
            headers={"X-Chaos-Token": self.settings.chaos_token.get_secret_value()},
            timeout=self.settings.chaos_request_timeout_seconds,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"target answered {resp.status_code}: {resp.text[:200]}")
        return f"target answered {resp.status_code}"
