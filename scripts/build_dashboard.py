"""Generates the Grafana dashboard JSON (dashboard-as-code).

    python scripts/build_dashboard.py

Writes helm/autoheal/files/dashboards/autoheal-overview.json, which is used
both by the Helm chart (Grafana sidecar ConfigMap) and docker compose.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "helm/autoheal/files/dashboards/autoheal-overview.json"
DS = {"type": "prometheus", "uid": "${datasource}"}
APP = 'job=~"autoheal-.*"'
NOT_PROBES = 'route!~"/health.*|/metrics"'

_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def target(expr: str, legend: str = "") -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A"}


def stat(title: str, expr: str, x: int, y: int, unit: str = "short", thresholds: list | None = None) -> dict:
    steps = thresholds or [{"color": "green", "value": None}]
    return {
        "id": _id(),
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": 4, "h": 4},
        "targets": [target(expr)],
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": [],
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "area"},
    }


def series(title: str, targets: list[dict], x: int, y: int, w: int = 12, unit: str = "short", stack: bool = False):
    return {
        "id": _id(),
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"fillOpacity": 15, "lineWidth": 2, "stacking": {"mode": "normal" if stack else "none"}},
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    }


def row(title: str, y: int) -> dict:
    return {
        "id": _id(),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    }


RED = [{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 5}]
ERR = [{"color": "green", "value": None}, {"color": "orange", "value": 0.01}, {"color": "red", "value": 0.05}]

panels = [
    row("Platform health", 0),
    stat("Targets up", f"sum(up{{{APP}}})", 0, 1),
    stat("Request rate", f"sum(rate(autoheal_http_requests_total{{{NOT_PROBES}}}[2m]))", 4, 1, "reqps"),
    stat(
        "5xx ratio",
        'sum(rate(autoheal_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(autoheal_http_requests_total[5m])), 1e-9)',
        8,
        1,
        "percentunit",
        ERR,
    ),
    stat(
        "Container restarts (1h)",
        'sum(increase(kube_pod_container_status_restarts_total{namespace="autoheal"}[1h]))',
        12,
        1,
        "short",
        RED,
    ),
    stat(
        "Jobs recovered from dead workers (1h)",
        'sum(increase(autoheal_jobs_requeued_total{reason="lease_expired"}[1h]))',
        16,
        1,
    ),
    stat(
        "Open circuits",
        "sum(autoheal_circuit_breaker_state == bool 2)",
        20,
        1,
        "short",
        [{"color": "green", "value": None}, {"color": "red", "value": 1}],
    ),
    row("Traffic (RED)", 5),
    series(
        "Requests / s by service",
        [target(f"sum by (job) (rate(autoheal_http_requests_total{{{NOT_PROBES}}}[2m]))", "{{job}}")],
        0,
        6,
        unit="reqps",
    ),
    series(
        "Responses by status class",
        [
            target(
                'sum by (status) (rate(label_replace(autoheal_http_requests_total, "status", "${1}xx", "status", "(.).*")[2m:]))',
                "{{status}}",
            )
        ],
        12,
        6,
        unit="reqps",
        stack=True,
    ),
    series(
        "Latency p50 / p95 / p99",
        [
            target(
                f"histogram_quantile({q}, sum by (le) (rate(autoheal_http_request_duration_seconds_bucket{{{NOT_PROBES}}}[5m])))",
                f"p{int(q * 100)}",
            )
            for q in (0.5, 0.95, 0.99)
        ],
        0,
        14,
        unit="s",
    ),
    series(
        "Rate limiting & upstreams",
        [
            target("sum by (decision) (rate(autoheal_rate_limit_decisions_total[2m]))", "ratelimit {{decision}}"),
            target(
                "sum by (upstream, outcome) (rate(autoheal_upstream_requests_total[2m]))", "{{upstream}} {{outcome}}"
            ),
        ],
        12,
        14,
        unit="reqps",
    ),
    row("Jobs", 22),
    series(
        "Job outcomes / s",
        [
            target("sum by (outcome) (rate(autoheal_jobs_finished_total[2m]))", "{{outcome}}"),
            target("sum(rate(autoheal_jobs_requeued_total[2m]))", "requeued"),
        ],
        0,
        23,
        w=8,
        unit="ops",
    ),
    series(
        "Queue depth",
        [
            target("max by (state) (autoheal_job_queue_depth)", "{{state}}"),
            target("sum(autoheal_jobs_in_progress)", "in progress"),
        ],
        8,
        23,
        w=8,
    ),
    series(
        "Job duration p95 by type",
        [
            target(
                "histogram_quantile(0.95, sum by (le, type) (rate(autoheal_job_duration_seconds_bucket[5m])))",
                "{{type}}",
            )
        ],
        16,
        23,
        w=8,
        unit="s",
    ),
    row("Kubernetes self-healing & scaling", 31),
    series(
        "Pods ready per deployment",
        [
            target('kube_deployment_status_replicas_ready{namespace="autoheal"}', "{{deployment}} ready"),
            target('kube_deployment_spec_replicas{namespace="autoheal"}', "{{deployment}} desired"),
        ],
        0,
        32,
        w=8,
    ),
    series(
        "Container restarts",
        [
            target(
                'sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="autoheal"}[5m]))', "{{pod}}"
            )
        ],
        8,
        32,
        w=8,
    ),
    series(
        "HPA replicas",
        [
            target(
                'kube_horizontalpodautoscaler_status_current_replicas{namespace="autoheal"}',
                "{{horizontalpodautoscaler}} current",
            ),
            target(
                'kube_horizontalpodautoscaler_status_desired_replicas{namespace="autoheal"}',
                "{{horizontalpodautoscaler}} desired",
            ),
        ],
        16,
        32,
        w=8,
    ),
    series(
        "CPU usage vs request (cores)",
        [
            target(
                'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="autoheal", container!=""}[2m]))',
                "{{pod}}",
            ),
            target("sum by (job) (rate(process_cpu_seconds_total{" + APP + "}[2m]))", "{{job}} (process)"),
        ],
        0,
        40,
        unit="short",
    ),
    series(
        "Memory working set",
        [
            target('sum by (pod) (container_memory_working_set_bytes{namespace="autoheal", container!=""})', "{{pod}}"),
            target("sum by (job) (process_resident_memory_bytes{" + APP + "})", "{{job}} (process)"),
        ],
        12,
        40,
        unit="bytes",
    ),
    row("Failure injection", 48),
    series(
        "Chaos injections & experiments",
        [
            target("sum by (kind) (increase(autoheal_chaos_injections_total[5m]))", "injected {{kind}}"),
            target("sum by (scenario) (increase(autoheal_chaos_experiments_total[5m]))", "experiment {{scenario}}"),
        ],
        0,
        49,
        w=24,
    ),
]

for panel in panels:
    for i, t in enumerate(panel.get("targets", [])):
        t["refId"] = chr(ord("A") + i)

dashboard = {
    "uid": "autoheal-overview",
    "title": "AutoHeal / Platform Overview",
    "tags": ["autoheal", "kubernetes", "self-healing"],
    "timezone": "browser",
    "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"},
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "templating": {
        "list": [
            {
                "name": "datasource",
                "type": "datasource",
                "query": "prometheus",
                "label": "Data source",
                "current": {"text": "Prometheus", "value": "prometheus"},
            }
        ]
    },
    "annotations": {
        "list": [
            {
                "name": "Failure injections",
                "datasource": DS,
                "enable": True,
                "iconColor": "red",
                "expr": "changes(autoheal_chaos_injections_total[1m]) > 0",
                "titleFormat": "chaos: {{kind}}",
            }
        ]
    },
    "panels": panels,
}

if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(OUT.parents[4])} ({len(panels)} panels)")
