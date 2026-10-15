"""Cluster access layer.

`ClusterClient` is the port the API depends on; `KubernetesCluster` is the
adapter for a real API server. The official client is synchronous, so calls
run in worker threads to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from autoheal_control.models import (
    AutoscalerStatus,
    ClusterEvent,
    Condition,
    DeploymentStatus,
    EventCategory,
    PodStatus,
    Termination,
)

log = logging.getLogger(__name__)


class ClusterUnavailable(Exception):
    """No cluster backend configured, or the API server could not be reached."""


class ClusterClient(Protocol):
    namespace: str

    async def list_deployments(self) -> list[DeploymentStatus]: ...
    async def list_pods(self, component: str | None = None) -> list[PodStatus]: ...
    async def list_autoscalers(self) -> list[AutoscalerStatus]: ...
    async def list_events(self, limit: int) -> list[ClusterEvent]: ...
    async def delete_pod(self, name: str) -> None: ...
    async def ping(self) -> None: ...


class NoCluster:
    """Used outside Kubernetes (docker compose): every call is unavailable."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    async def _fail(self, *_: Any, **__: Any) -> Any:
        raise ClusterUnavailable("no Kubernetes backend configured (CLUSTER_BACKEND=none)")

    list_deployments = list_autoscalers = ping = _fail

    async def list_pods(self, component: str | None = None) -> list[PodStatus]:
        return await self._fail()

    async def list_events(self, limit: int) -> list[ClusterEvent]:
        return await self._fail()

    async def delete_pod(self, name: str) -> None:
        await self._fail()


# --- event classification ---------------------------------------------

_REASON_CATEGORY = {
    "Killing": EventCategory.restart,
    "Started": EventCategory.restart,
    "Unhealthy": EventCategory.probe,
    "ProbeWarning": EventCategory.probe,
    "BackOff": EventCategory.crashloop,
    "CrashLoopBackOff": EventCategory.crashloop,
    "OOMKilling": EventCategory.oom,
    "OOMKilled": EventCategory.oom,
    "SuccessfulCreate": EventCategory.replacement,
    "SuccessfulDelete": EventCategory.replacement,
    "ScalingReplicaSet": EventCategory.scaling,
    "SuccessfulRescale": EventCategory.scaling,
    "Scheduled": EventCategory.scheduling,
    "FailedScheduling": EventCategory.scheduling,
    "Evicted": EventCategory.replacement,
    "Preempting": EventCategory.replacement,
}
RECOVERY_CATEGORIES = frozenset(EventCategory) - {EventCategory.other, EventCategory.scheduling}


def classify(reason: str, message: str) -> EventCategory:
    if reason == "Killing" and "liveness" in message.lower():
        return EventCategory.probe
    if reason == "Started" and not re.search(r"container", message, re.I):
        return EventCategory.other
    return _REASON_CATEGORY.get(reason, EventCategory.other)


# --- converters (kubernetes client objects -> API models) ----------------


def _conditions(raw: list[Any] | None) -> list[Condition]:
    return [Condition(type=c.type, status=c.status, reason=c.reason, message=c.message) for c in raw or []]


def deployment_from_k8s(d: Any, component_label: str) -> DeploymentStatus:
    status = d.status
    return DeploymentStatus(
        name=d.metadata.name,
        component=(d.metadata.labels or {}).get(component_label),
        desired=d.spec.replicas or 0,
        ready=status.ready_replicas or 0,
        available=status.available_replicas or 0,
        updated=status.updated_replicas or 0,
        images=[c.image for c in d.spec.template.spec.containers],
        conditions=_conditions(status.conditions),
        created_at=d.metadata.creation_timestamp,
    )


def pod_from_k8s(p: Any, component_label: str) -> PodStatus:
    statuses = p.status.container_statuses or []
    waiting = next((cs.state.waiting.reason for cs in statuses if cs.state and cs.state.waiting), None)
    last = None
    for cs in statuses:
        term = cs.last_state.terminated if cs.last_state else None
        if term and (last is None or (term.finished_at and last.finished_at and term.finished_at > last.finished_at)):
            last = Termination(reason=term.reason, exit_code=term.exit_code, finished_at=term.finished_at)
    return PodStatus(
        name=p.metadata.name,
        component=(p.metadata.labels or {}).get(component_label),
        phase=p.status.phase or "Unknown",
        ready=bool(statuses) and all(cs.ready for cs in statuses),
        restarts=sum(cs.restart_count or 0 for cs in statuses),
        node=p.spec.node_name,
        pod_ip=p.status.pod_ip,
        started_at=p.status.start_time,
        terminating=p.metadata.deletion_timestamp is not None,
        waiting_reason=waiting,
        last_termination=last,
    )


def autoscaler_from_k8s(h: Any) -> AutoscalerStatus:
    target_cpu = current_cpu = None
    for m in h.spec.metrics or []:
        if m.type == "Resource" and m.resource.name == "cpu":
            target_cpu = m.resource.target.average_utilization
    for m in (h.status.current_metrics or []) if h.status else []:
        if m.type == "Resource" and m.resource and m.resource.name == "cpu":
            current_cpu = m.resource.current.average_utilization
    return AutoscalerStatus(
        name=h.metadata.name,
        target=h.spec.scale_target_ref.name,
        min_replicas=h.spec.min_replicas or 1,
        max_replicas=h.spec.max_replicas,
        current_replicas=h.status.current_replicas or 0,
        desired_replicas=h.status.desired_replicas or 0,
        target_cpu_utilization=target_cpu,
        current_cpu_utilization=current_cpu,
        last_scale_time=h.status.last_scale_time,
    )


def event_from_k8s(e: Any) -> ClusterEvent:
    ts = e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp
    reason, message = e.reason or "", (e.message or "").strip()
    return ClusterEvent(
        timestamp=ts,
        type=e.type or "Normal",
        reason=reason,
        category=classify(reason, message),
        object_kind=e.involved_object.kind if e.involved_object else None,
        object_name=e.involved_object.name if e.involved_object else None,
        message=message,
        count=e.count or 1,
    )


_EPOCH = datetime.min.replace(tzinfo=UTC)


class KubernetesCluster:
    def __init__(self, namespace: str, selector: str, component_label: str) -> None:
        from kubernetes import client, config  # heavy import: only when actually used

        try:
            if os.getenv("KUBERNETES_SERVICE_HOST"):
                config.load_incluster_config()
            else:
                config.load_kube_config()
        except config.ConfigException as exc:
            raise ClusterUnavailable(f"no Kubernetes configuration found: {exc}") from exc

        self.namespace = namespace
        self.selector = selector
        self.component_label = component_label
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._autoscaling = client.AutoscalingV2Api()
        self._api_exception = client.exceptions.ApiException

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except self._api_exception as exc:
            raise ClusterUnavailable(f"Kubernetes API error {exc.status}: {exc.reason}") from exc
        except Exception as exc:  # urllib3 connection errors etc.
            raise ClusterUnavailable(f"Kubernetes API unreachable: {type(exc).__name__}") from exc

    async def ping(self) -> None:
        await self._call(self._core.list_namespaced_pod, self.namespace, limit=1, _request_timeout=2)

    async def list_deployments(self) -> list[DeploymentStatus]:
        res = await self._call(self._apps.list_namespaced_deployment, self.namespace, label_selector=self.selector)
        return sorted((deployment_from_k8s(d, self.component_label) for d in res.items), key=lambda d: d.name)

    async def list_pods(self, component: str | None = None) -> list[PodStatus]:
        selector = self.selector + (f",{self.component_label}={component}" if component else "")
        res = await self._call(self._core.list_namespaced_pod, self.namespace, label_selector=selector)
        return sorted((pod_from_k8s(p, self.component_label) for p in res.items), key=lambda p: p.name)

    async def list_autoscalers(self) -> list[AutoscalerStatus]:
        res = await self._call(
            self._autoscaling.list_namespaced_horizontal_pod_autoscaler, self.namespace, label_selector=self.selector
        )
        return [autoscaler_from_k8s(h) for h in res.items]

    async def list_events(self, limit: int) -> list[ClusterEvent]:
        res = await self._call(self._core.list_namespaced_event, self.namespace)
        events = [event_from_k8s(e) for e in res.items]
        events.sort(key=lambda e: e.timestamp or _EPOCH, reverse=True)
        return events[:limit]

    async def delete_pod(self, name: str) -> None:
        await self._call(self._core.delete_namespaced_pod, name, self.namespace, grace_period_seconds=0)


def build_cluster(backend: str, namespace: str, selector: str, component_label: str) -> ClusterClient:
    if backend == "none":
        return NoCluster(namespace)
    try:
        return KubernetesCluster(namespace, selector, component_label)
    except ClusterUnavailable as exc:
        log.error("cluster backend unavailable; cluster views disabled", extra={"error": str(exc)})
        return NoCluster(namespace)
