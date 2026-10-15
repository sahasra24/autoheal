from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class Condition(BaseModel):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class DeploymentStatus(BaseModel):
    name: str
    component: str | None
    desired: int
    ready: int
    available: int
    updated: int
    images: list[str] = []
    conditions: list[Condition] = []
    created_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def healthy(self) -> bool:
        return self.desired == self.ready == self.available


class Termination(BaseModel):
    reason: str | None = None
    exit_code: int | None = None
    finished_at: datetime | None = None


class PodStatus(BaseModel):
    name: str
    component: str | None
    phase: str
    ready: bool
    restarts: int
    node: str | None = None
    pod_ip: str | None = None
    started_at: datetime | None = None
    terminating: bool = False
    waiting_reason: str | None = Field(default=None, description="e.g. CrashLoopBackOff, ContainerCreating")
    last_termination: Termination | None = None


class AutoscalerStatus(BaseModel):
    name: str
    target: str
    min_replicas: int
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    target_cpu_utilization: int | None = None
    current_cpu_utilization: int | None = None
    last_scale_time: datetime | None = None


class EventCategory(StrEnum):
    restart = "restart"  # container killed / restarted
    probe = "probe"  # liveness/readiness/startup probe failures
    crashloop = "crashloop"
    oom = "oom"
    replacement = "replacement"  # controller created/deleted pods
    scaling = "scaling"  # HPA / deployment scale
    scheduling = "scheduling"
    other = "other"


class ClusterEvent(BaseModel):
    timestamp: datetime | None
    type: str  # Normal | Warning
    reason: str
    category: EventCategory
    object_kind: str | None
    object_name: str | None
    message: str
    count: int = 1


class Overview(BaseModel):
    namespace: str
    healthy: bool
    deployments: list[DeploymentStatus]
    pods_total: int
    pods_ready: int
    total_restarts: int
    autoscalers: list[AutoscalerStatus]
    recent_recovery_events: int


# --- failure injection --------------------------------------------------


class Scenario(StrEnum):
    crash = "crash"
    unhealthy = "unhealthy"
    unready = "unready"
    cpu_stress = "cpu_stress"
    memory_leak = "memory_leak"
    latency = "latency"
    error_rate = "error_rate"
    reset = "reset"
    pod_delete = "pod_delete"


class ExperimentRequest(BaseModel):
    scenario: Scenario
    target: str = Field(default="tasks", description="Component label value, e.g. tasks, gateway, redis")
    pod: str | None = Field(default=None, description="Specific pod; default: a random ready pod")
    params: dict[str, Any] = {}


class Experiment(BaseModel):
    id: str
    scenario: Scenario
    target: str
    pod: str | None
    params: dict[str, Any]
    status: str  # injected | failed
    detail: str
    expected_recovery: str
    created_at: datetime
    restarts_before: int | None = None
