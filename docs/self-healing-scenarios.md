# Self-healing scenarios

Each scenario injects one real failure and shows which Kubernetes mechanism repairs it. Run them through the dashboard (http://localhost:8090), the CLI (`python scripts/chaos_demo.py <scenario>`), or raw `kubectl`. The CLI prints how long detection and recovery took.

Setup for every scenario:

```bash
make port-forward                # in one terminal
export CHAOS_TOKEN=$(kubectl -n autoheal get secret autoheal-secrets -o jsonpath='{.data.chaos-token}' | base64 -d)
kubectl -n autoheal get pods -w  # in another terminal: watch it happen
```

Probe timings (`values.yaml`) set how fast detection can be: liveness checks every 10s and restarts after 3 failures (~30s); readiness checks every 5s and is removed after 2 failures (~10s).

---

## 1. Container crash

```bash
python scripts/chaos_demo.py crash
```

**Fault:** the process calls `os._exit(1)` half a second after acknowledging the request.
**Mechanism:** the container's main process exits, and the kubelet restarts it (`restartPolicy: Always`). `RESTARTS` goes up by one, and `lastState.terminated.reason` is `Error`.
**What to watch:** the pod briefly shows `0/1 Running`. The startup probe passes, then readiness, then the pod rejoins the Service. Other replicas take the traffic meanwhile, so the gateway serves no errors. Crash it repeatedly and you'll see `CrashLoopBackOff`: the kubelet backs off exponentially (10s, 20s, 40s and so on, up to 5 min) to stop a broken app from thrashing the node. The `AutoHealCrashLooping` alert fires if that lasts 5 minutes.

## 2. Unhealthy application (liveness)

```bash
python scripts/chaos_demo.py unhealthy
```

**Fault:** the process keeps running and keeps serving, but `/health/live` returns 503. This simulates a deadlock or wedged event loop that a crash-based check would miss.
**Mechanism:** after 3 consecutive liveness failures, the kubelet kills the container and starts a new one. The event log shows `Unhealthy: Liveness probe failed` and then `Killing: Container tasks failed liveness probe, will be restarted`.
**Expected time:** ~30 to 40s (3 × 10s period, plus restart and startup).

## 3. Unready application (readiness)

```bash
python scripts/chaos_demo.py unready --seconds 30
```

**Fault:** `/health/ready` returns 503 for 30s.
**Mechanism:** the endpoints controller removes the pod from the Service. **It is not restarted.** Readiness means "don't send me traffic", not "I'm broken". When the window ends, the pod is added back automatically.
**What to watch:** `kubectl -n autoheal get endpoints autoheal-tasks -w` shows the pod's IP disappear and come back. The restart count stays the same.

## 4. Pod deletion (replica replacement)

```bash
python scripts/chaos_demo.py pod_delete
```

**Fault:** the pod is deleted through the Kubernetes API with grace period 0, which mimics a node failure or an accidental `kubectl delete`.
**Mechanism:** the ReplicaSet sees `current < desired` and creates a replacement, which the scheduler places (topology spread prefers a different node). Events: `SuccessfulCreate` on the ReplicaSet.
**Note:** if the pod was running a job, that job is recovered too (see scenario 8).

## 5. Memory leak (OOMKilled)

```bash
python scripts/chaos_demo.py memory_leak --mb 1024
```

**Fault:** the process allocates and touches 1 GiB, which is above the 512 MiB container limit.
**Mechanism:** the kernel cgroup OOM killer kills the process (exit code 137), and the kubelet records `OOMKilled` and restarts the container. The control service's recovery timeline shows an `oom` entry, and `AutoHealOOMKilled` fires.
**Takeaway:** limits protect the node and neighbouring pods. One leaky pod can't take down the machine.

## 6. Traffic spike (Horizontal Pod Autoscaler)

Requires metrics-server (`make cluster-up` installs it).

```bash
python scripts/chaos_demo.py cpu_stress --seconds 180     # synthetic: burn CPU in one pod
make loadtest                                              # realistic: Locust step load via the gateway
kubectl -n autoheal get hpa -w
```

**Mechanism:** metrics-server reports CPU against the pod's *request* (250m). Above 60% average, the HPA raises `desiredReplicas` (scale-up policy: up to +100% or +2 pods every 30s). New pods start, pass readiness and start pulling jobs from the shared queue. When load drops, the HPA waits for the 120s stabilisation window and then removes at most 50% of pods per minute, which avoids flapping.
**What to watch:** the Grafana *HPA replicas* and *CPU usage* panels, plus the Locust p95 latency, which should level off as replicas join.

## 7. Dependency outage (Redis)

```bash
python scripts/chaos_demo.py pod_delete --target redis
```

**Fault:** the Redis pod disappears.
**Mechanism, in layers:**
- Tasks pods fail the `redis` readiness check and go **unready, not restarted**. Liveness doesn't depend on Redis, so there is no restart storm.
- Workers back off exponentially instead of spinning on errors.
- Gateway: the rate limiter **fails open** (`X-RateLimit-Degraded: true`) and the gateway stays ready. Job calls get a fast `503` (the circuit breaker opens) instead of hanging.
- The Redis Deployment schedules a replacement. Once it passes its probes, tasks pods turn ready again with no human involved.
**Note:** with `redis.persistence.enabled=false` (local default), queued jobs are lost with the pod. Turn on persistence (it's on in `values-eks.yaml`) to keep them.

## 8. Worker dies mid-job (application-level healing)

Kubernetes restarts the *pod*. The reliable queue recovers the *work*.

```bash
# 1. submit a long job
JOB=$(curl -s -X POST localhost:8080/api/v1/jobs -H 'Content-Type: application/json' \
      -d '{"type":"sleep","params":{"seconds":45}}' | python -c 'import sys,json; print(json.load(sys.stdin)["id"])')
sleep 3
# 2. find which pod is running it, and crash that pod
POD=$(curl -s localhost:8080/api/v1/jobs/$JOB | python -c 'import sys,json; print(json.load(sys.stdin)["worker"])')
curl -s -X POST localhost:8090/api/v1/chaos/experiments -H "X-Chaos-Token: $CHAOS_TOKEN" \
     -H 'Content-Type: application/json' -d "{\"scenario\":\"crash\",\"target\":\"tasks\",\"pod\":\"$POD\"}"
# 3. watch it come back
watch -n2 "curl -s localhost:8080/api/v1/jobs/$JOB"
```

**Expected:** within about 15 to 20s (lease TTL plus reaper interval), the job's `error` reads `worker lost; requeued`, its status goes back to `queued` and then `running` on a **different** worker with `attempts: 2`, and it finishes as `succeeded`. Prometheus counts it in `autoheal_jobs_requeued_total{reason="lease_expired"}`.

## 9. Bad deployment (rollout safety)

```bash
# Deploy an image tag that doesn't exist, or one whose probes never pass:
helm upgrade autoheal helm/autoheal -n autoheal --reuse-values --set image.tag=broken --atomic --wait --timeout 2m
```

**Mechanism:** with `maxSurge: 1, maxUnavailable: 0`, Kubernetes starts one new pod and doesn't remove any old pod until the new one is Ready. The new pod never becomes Ready (`ImagePullBackOff` or failing probes), so the old version keeps serving 100% of traffic. `--atomic` makes Helm roll the release back automatically when the wait times out. CD uses the same flag on EKS.

---

## Record your results

Run `make demo` (scenarios 1 to 5) on your cluster and fill this in. These are your numbers to quote on a resume:

| Scenario | Detected | Recovered | Notes |
|---|---|---|---|
| Container crash | | | |
| Liveness failure | | | |
| Readiness failure | | | no restart |
| Pod deletion | | | |
| OOMKilled | | | |
| HPA scale-out (2 → N) | | | Locust users / RPS at peak: |
| Redis outage | | | gateway 5xx during outage: |
| Job recovered from dead worker | | | |

Environment: minikube/kind version, node count, CPU/RAM.
