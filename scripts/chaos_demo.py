#!/usr/bin/env python3
"""Run a self-healing scenario and measure how long Kubernetes takes to recover.

Standard library only. Talks to the control service, so port-forward it first:

    kubectl -n autoheal port-forward svc/autoheal-control 8090:80
    export CHAOS_TOKEN=$(kubectl -n autoheal get secret autoheal-secrets -o jsonpath='{.data.chaos-token}' | base64 -d)

    python scripts/chaos_demo.py crash
    python scripts/chaos_demo.py unhealthy
    python scripts/chaos_demo.py unready --seconds 30
    python scripts/chaos_demo.py memory_leak --mb 1024
    python scripts/chaos_demo.py pod_delete
    python scripts/chaos_demo.py pod_delete --target redis
    python scripts/chaos_demo.py cpu_stress --seconds 180
    python scripts/chaos_demo.py all            # every scenario, then a summary table
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    scenario: str
    target: str
    pod: str | None
    detected_s: float | None = None
    recovered_s: float | None = None
    notes: list[str] = field(default_factory=list)


class Control:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Chaos-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}")
        except urllib.error.URLError as exc:
            sys.exit(f"cannot reach control service at {self.url} ({exc.reason}). Is the port-forward running?")

    def pods(self, component: str) -> list[dict[str, Any]]:
        return self._req("GET", f"/api/v1/pods?component={component}")

    def overview(self) -> dict[str, Any]:
        return self._req("GET", "/api/v1/overview")

    def experiment(self, scenario: str, target: str, pod: str | None, params: dict[str, Any]) -> dict[str, Any]:
        return self._req(
            "POST", "/api/v1/chaos/experiments", {"scenario": scenario, "target": target, "pod": pod, "params": params}
        )


def log(t0: float, msg: str) -> None:
    print(f"  [{time.monotonic() - t0:6.1f}s] {msg}", flush=True)


def wait(t0: float, timeout: float, check, interval: float = 1.0) -> float | None:
    while time.monotonic() - t0 < timeout:
        if check():
            return round(time.monotonic() - t0, 1)
        time.sleep(interval)
    return None


def pick_ready_pod(control: Control, target: str) -> dict[str, Any]:
    ready = [p for p in control.pods(target) if p["ready"] and not p["terminating"]]
    if not ready:
        sys.exit(f"no ready '{target}' pods - is the platform healthy?")
    return ready[0]


def run(control: Control, scenario: str, target: str, params: dict[str, Any], timeout: float) -> Result:
    print(f"\n=== {scenario} -> {target} ===")
    pod = pick_ready_pod(control, target)
    baseline_ready = sum(p["ready"] for p in control.pods(target))
    name, restarts_before = pod["name"], pod["restarts"]
    result = Result(scenario, target, name)

    exp = control.experiment(scenario, target, name, params)
    t0 = time.monotonic()
    print(f"  injected into {name}: {exp['detail']}")
    print(f"  expected: {exp['expected_recovery']}")
    if exp["status"] != "injected":
        result.notes.append("injection failed")
        return result

    def this_pod() -> dict[str, Any] | None:
        return next((p for p in control.pods(target) if p["name"] == name), None)

    if scenario in ("crash", "unhealthy", "memory_leak"):
        # Detected = pod left the load balancer, or (fast restarts) the restart already happened.
        result.detected_s = wait(
            t0,
            timeout,
            lambda: (p := this_pod()) is not None and (not p["ready"] or p["restarts"] > restarts_before),
            0.5,
        )
        log(t0, f"failure detected on {name} (not ready or already restarted)")
        result.recovered_s = wait(
            t0, timeout, lambda: (p := this_pod()) is not None and p["restarts"] > restarts_before and p["ready"]
        )
        p = this_pod() or {}
        term = (p.get("last_termination") or {}).get("reason")
        log(
            t0, f"container restarted by kubelet (restarts {restarts_before} -> {p.get('restarts')}, last exit: {term})"
        )
        if term:
            result.notes.append(f"last termination: {term}")

    elif scenario == "unready":
        result.detected_s = wait(t0, timeout, lambda: (p := this_pod()) is not None and not p["ready"], 0.5)
        log(t0, f"{name} removed from Service endpoints; no restart")
        result.recovered_s = wait(t0, timeout, lambda: (p := this_pod()) is not None and p["ready"])
        p = this_pod() or {}
        log(t0, f"{name} ready again, restarts still {p.get('restarts')}")

    elif scenario == "pod_delete":
        result.detected_s = wait(t0, timeout, lambda: this_pod() is None or this_pod()["terminating"], 0.5)
        log(t0, f"{name} deleted")
        result.recovered_s = wait(
            t0,
            timeout,
            lambda: (
                this_pod() is None
                and sum(p["ready"] and not p["terminating"] for p in control.pods(target)) >= baseline_ready
            ),
        )
        new = [p["name"] for p in control.pods(target) if p["name"] != name]
        log(t0, f"ReplicaSet restored {baseline_ready} ready pod(s): {', '.join(new)}")
        if target == "redis":
            healthy = wait(t0, timeout, lambda: control.overview()["healthy"])
            log(t0, f"dependent services ready again (platform healthy at {healthy}s)")

    elif scenario == "cpu_stress":
        hpa = next((h for h in control.overview()["autoscalers"] if h["target"].endswith(target)), None)
        if hpa is None:
            sys.exit("no HPA found for target (is metrics-server installed?)")
        start = hpa["current_replicas"]
        log(t0, f"HPA {hpa['name']}: {start} replicas, target CPU {hpa['target_cpu_utilization']}%")

        def scaled() -> bool:
            h = next(h for h in control.overview()["autoscalers"] if h["name"] == hpa["name"])
            log(t0, f"cpu {h['current_cpu_utilization']}%  replicas {h['current_replicas']} -> {h['desired_replicas']}")
            return h["current_replicas"] > start

        result.recovered_s = wait(t0, timeout, scaled, 10)
        result.notes.append("time to scale out" if result.recovered_s else "did not scale within timeout")

    else:
        log(t0, "fault applied; watch Grafana / the control dashboard for alerts")

    status = f"recovered in {result.recovered_s}s" if result.recovered_s is not None else "NOT recovered within timeout"
    print(f"  => {status}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "scenario",
        choices=[
            "crash",
            "unhealthy",
            "unready",
            "memory_leak",
            "pod_delete",
            "cpu_stress",
            "latency",
            "error_rate",
            "reset",
            "all",
        ],
    )
    parser.add_argument("--target", default="tasks")
    parser.add_argument("--control", default=os.getenv("CONTROL_URL", "http://localhost:8090"))
    parser.add_argument("--token", default=os.getenv("CHAOS_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--seconds", type=float, help="duration for unready / cpu_stress")
    parser.add_argument("--mb", type=int, default=1024, help="memory to allocate for memory_leak")
    parser.add_argument("--ms", type=int, default=1500, help="latency to inject")
    parser.add_argument("--rate", type=float, default=0.5, help="error rate to inject")
    args = parser.parse_args()
    if not args.token:
        sys.exit("set CHAOS_TOKEN or pass --token (see the module docstring)")

    params = {
        "unready": {"seconds": args.seconds or 30},
        "cpu_stress": {"seconds": args.seconds or 180, "workers": 2},
        "memory_leak": {"mb": args.mb},
        "latency": {"ms": args.ms},
        "error_rate": {"rate": args.rate},
    }
    control = Control(args.control, args.token)
    scenarios = (
        ["crash", "unhealthy", "unready", "memory_leak", "pod_delete"] if args.scenario == "all" else [args.scenario]
    )
    results = []
    for s in scenarios:
        results.append(run(control, s, args.target, params.get(s, {}), args.timeout))
        if args.scenario == "all":
            wait(time.monotonic(), args.timeout, lambda: control.overview()["healthy"], 2)

    if len(results) > 1:
        print("\nScenario        Pod                                   Detected   Recovered  Notes")
        for r in results:
            det = f"{r.detected_s}s" if r.detected_s is not None else "-"
            rec = f"{r.recovered_s}s" if r.recovered_s is not None else "FAILED"
            print(f"{r.scenario:<15} {r.pod or '-':<37} {det:<10} {rec:<10} {'; '.join(r.notes)}")
    sys.exit(
        0 if all(r.recovered_s is not None or r.scenario in ("latency", "error_rate", "reset") for r in results) else 1
    )


if __name__ == "__main__":
    main()
