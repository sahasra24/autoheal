"""Converters tested against real kubernetes-client model objects."""

from __future__ import annotations

from datetime import UTC, datetime

from kubernetes import client as k8s

from autoheal_control.cluster import autoscaler_from_k8s, classify, event_from_k8s, pod_from_k8s
from autoheal_control.models import EventCategory

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LABELS = {"app.kubernetes.io/component": "tasks"}


def test_pod_with_oomkilled_restart() -> None:
    pod = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="tasks-1", labels=LABELS),
        spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="tasks")], node_name="node-a"),
        status=k8s.V1PodStatus(
            phase="Running",
            pod_ip="10.1.2.3",
            start_time=T0,
            container_statuses=[
                k8s.V1ContainerStatus(
                    name="tasks",
                    image="autoheal-tasks:1",
                    image_id="",
                    ready=True,
                    restart_count=3,
                    state=k8s.V1ContainerState(running=k8s.V1ContainerStateRunning(started_at=T0)),
                    last_state=k8s.V1ContainerState(
                        terminated=k8s.V1ContainerStateTerminated(exit_code=137, reason="OOMKilled", finished_at=T0)
                    ),
                )
            ],
        ),
    )
    p = pod_from_k8s(pod, "app.kubernetes.io/component")
    assert (p.name, p.component, p.ready, p.restarts, p.node) == ("tasks-1", "tasks", True, 3, "node-a")
    assert p.last_termination.reason == "OOMKilled" and p.last_termination.exit_code == 137
    assert p.waiting_reason is None and not p.terminating


def test_pod_in_crashloop() -> None:
    pod = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="tasks-2", labels=LABELS, deletion_timestamp=T0),
        spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="tasks")]),
        status=k8s.V1PodStatus(
            phase="Running",
            container_statuses=[
                k8s.V1ContainerStatus(
                    name="tasks",
                    image="x",
                    image_id="",
                    ready=False,
                    restart_count=5,
                    state=k8s.V1ContainerState(waiting=k8s.V1ContainerStateWaiting(reason="CrashLoopBackOff")),
                )
            ],
        ),
    )
    p = pod_from_k8s(pod, "app.kubernetes.io/component")
    assert p.waiting_reason == "CrashLoopBackOff" and not p.ready and p.terminating


def test_autoscaler() -> None:
    hpa = k8s.V2HorizontalPodAutoscaler(
        metadata=k8s.V1ObjectMeta(name="autoheal-tasks"),
        spec=k8s.V2HorizontalPodAutoscalerSpec(
            scale_target_ref=k8s.V2CrossVersionObjectReference(kind="Deployment", name="autoheal-tasks"),
            min_replicas=2,
            max_replicas=10,
            metrics=[
                k8s.V2MetricSpec(
                    type="Resource",
                    resource=k8s.V2ResourceMetricSource(
                        name="cpu", target=k8s.V2MetricTarget(type="Utilization", average_utilization=60)
                    ),
                )
            ],
        ),
        status=k8s.V2HorizontalPodAutoscalerStatus(
            current_replicas=4,
            desired_replicas=6,
            current_metrics=[
                k8s.V2MetricStatus(
                    type="Resource",
                    resource=k8s.V2ResourceMetricStatus(
                        name="cpu", current=k8s.V2MetricValueStatus(average_utilization=95)
                    ),
                )
            ],
        ),
    )
    a = autoscaler_from_k8s(hpa)
    assert (a.current_replicas, a.desired_replicas, a.target_cpu_utilization, a.current_cpu_utilization) == (
        4,
        6,
        60,
        95,
    )


def test_event_conversion_and_classification() -> None:
    evt = k8s.CoreV1Event(
        metadata=k8s.V1ObjectMeta(name="e1"),
        involved_object=k8s.V1ObjectReference(kind="Pod", name="tasks-1"),
        reason="Killing",
        message="Container tasks failed liveness probe, will be restarted",
        type="Normal",
        last_timestamp=T0,
        count=2,
    )
    e = event_from_k8s(evt)
    assert e.category == EventCategory.probe and e.count == 2 and e.object_name == "tasks-1"


def test_classify_table() -> None:
    assert classify("BackOff", "Back-off restarting failed container") == EventCategory.crashloop
    assert classify("SuccessfulRescale", "New size: 4") == EventCategory.scaling
    assert classify("SuccessfulCreate", "Created pod: x") == EventCategory.replacement
    assert classify("Killing", "Stopping container tasks") == EventCategory.restart
    assert classify("Pulled", "already present") == EventCategory.other
