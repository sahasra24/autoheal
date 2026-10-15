# Architecture and design decisions

## Components

| Component | Responsibility | Scales | Talks to |
|---|---|---|---|
| **Gateway** | Single entry point. Validates payloads with the same Pydantic contract the backend uses, so bad requests never consume backend capacity. Optional API-key auth, distributed rate limiting, correlation IDs, per-upstream circuit breakers. | HPA 2–6 on CPU | tasks, control, Redis |
| **Tasks** | Accepts jobs, processes them from a reliable Redis queue, serves synchronous compute. Hosts the failure-injection endpoints used in demos. | HPA 2–10 on CPU | Redis |
| **Control** | Read-only view of the namespace through the Kubernetes API: deployments, pods, HPAs, events, and a merged recovery timeline. Runs failure-injection experiments against one specific pod. Serves the dashboard. | 1 replica | Kubernetes API, tasks/gateway pods |
| **Redis** | Job records, queue, worker leases, rate-limit windows. | 1 replica (`Recreate`) | — |

All three Python services share `libs/common`, so they behave identically in the ways operators care about: log format, metric names, health semantics and shutdown behaviour.

## Health model: three probes, three different questions

| Probe | Endpoint | Fails when | Kubernetes action |
|---|---|---|---|
| startup | `/health/startup` | lifespan hasn't finished initialising | holds off liveness/readiness (slow boots aren't killed) |
| liveness | `/health/live` | **the process itself** is broken (forced by failure injection, or the worker process pool is unrecoverable) | kill and restart the container |
| readiness | `/health/ready` | a **critical dependency** is down, the pod is shutting down, or readiness is forced off | remove from Service endpoints, no restart |

**Liveness never checks dependencies.** If Redis goes down and liveness depended on it, every tasks pod would be restarted over and over. That restart storm fixes nothing and makes recovery slower. Instead, tasks pods go *unready* (they can't do useful work), stay alive, and rejoin by themselves when Redis is back. Readiness checks run concurrently, each with a timeout, so one hung dependency can't make the probe time out.

The gateway treats Redis as **non-critical**: it reports `degraded` but stays ready, because its only Redis use is rate limiting (see below). The control service treats the Kubernetes API the same way.

## Reliable job queue

A naive `BRPOP` queue loses any job that a worker had popped when its pod died. AutoHeal uses the reliable-queue pattern:

1. **Claim**: `BLMOVE queue → processing` atomically. The job is never only in a worker's memory.
2. **Lease**: the worker sets `job:{id}:lease` with a TTL (15s) and renews it every TTL/3 from a heartbeat task.
3. **Finish**: on success or permanent failure, remove the job from `processing` and delete the lease. Retryable failures go back on the queue until `max_attempts`.
4. **Reap**: every pod runs a reaper. A job sitting in `processing` with no lease belongs to a dead worker. `LREM processing 1 id` returns 1 for exactly one reaper, so only that pod re-queues it, even with 10 replicas reaping at once.
5. **Fencing**: the attempt counter acts as a fencing token. If a slow worker's lease expires and the job runs again elsewhere, the stale worker's `complete()` sees a newer attempt number and its result is discarded.
6. **Graceful shutdown**: on SIGTERM, a pod stops claiming work, gives in-flight jobs up to 20s, then **releases** the rest back to the front of the queue without using up an attempt. A rolling deploy therefore costs no job attempts.

Delivery is **at-least-once**, which is the honest guarantee for any queue whose consumers can die. Workloads are idempotent.

## CPU work runs in a process pool

CPU-bound workloads (`prime_count`, `hash_chain`, `/compute`) run in a `ProcessPoolExecutor` using `spawn`. If they ran on the event loop, a saturated pod would stop answering `/health/live` and Kubernetes would kill a pod that was healthy, just busy. That is a classic self-inflicted outage under load. Separate processes also sidestep the GIL, so a pod can actually use the CPU its HPA target assumes.

If a pool process is killed (for example by the OOM killer), Python marks the pool permanently broken. The worker detects `BrokenProcessPool` and **fails liveness on purpose**, so Kubernetes replaces the pod with a clean one. This is self-healing used deliberately, not just put up with.

## Rate limiting

A sliding-window log in a Redis sorted set per client: trim entries older than the window, count what's left, add this request, all in one `MULTI` transaction. Because every gateway replica shares the same key, the limit is global no matter which pod a request reaches, and there is no fixed-window boundary burst. Rejected requests don't use up quota.

**It fails open** (configurable). If Redis is unreachable, requests are allowed, the response is marked `X-RateLimit-Degraded: true`, a metric goes up and an alert fires. Turning a cache outage into a total API outage is the worse failure mode for this system. Set `RATE_LIMIT_FAIL_OPEN=false` where abuse protection matters more than availability.

Identity is the API key (hashed, never stored raw) if one is sent, otherwise the client IP. `X-Forwarded-For` is trusted only when explicitly enabled (behind the ALB).

## Circuit breaker

Kubernetes heals the *callee*. The breaker protects the *caller*. While every tasks pod is restarting, the gateway would otherwise hold connections open until timeouts expire. After 5 consecutive failures (connection errors, timeouts or 5xx), the breaker opens and the gateway fails fast with `503 + Retry-After`. After 15s it lets exactly one trial request through (half-open): success closes it, failure re-opens it. Idempotent `GET`s retry once on connection failure, which covers the common case of reaching a pod that just terminated during a rolling update.

## Failure-injection safety

Failure injection is powerful, so it has layered guards:

- Off unless `CHAOS_ENABLED=true` **and** a token is set. It can never turn on when `ENVIRONMENT=production`; that is enforced in code, not just config.
- Every call needs `X-Chaos-Token` (constant-time comparison).
- Not routed through the gateway. It is reachable only inside the cluster or via `kubectl port-forward`.
- The control service only targets allow-listed components, resolves a **specific pod**, and calls that pod's IP directly. The NetworkPolicy allows control → tasks/gateway, and nothing else can reach those ports.
- The RBAC verb `delete pods` is granted only while chaos is enabled, and only in the platform's own namespace.
- Latency and error injection apply only to `/api/*`, so probes and metrics keep telling the truth while the API misbehaves.

## Observability

- **Logs**: one JSON object per line, with `service`, `correlation_id`, `pod` and structured extras. The gateway accepts or generates `X-Correlation-ID`, forwards it upstream, and the task service stores it on the job. A worker processing that job hours later logs with the original request's ID.
- **Metrics**: RED metrics keyed by **route template** (`/api/v1/jobs/{job_id}`, never raw paths, so cardinality stays bounded). Plus job outcomes, durations, queue depth, requeues, rate-limit decisions, breaker state, upstream latency and chaos injections. Kubernetes-level signals (restarts, ready replicas, HPA state) come from kube-state-metrics.
- **Alerts**: 13 rules in three groups (services, jobs, kubernetes), stored once in `helm/autoheal/files/alerts/` and shared by Compose and the PrometheusRule.
- **Dashboard**: generated by `scripts/build_dashboard.py`. CI fails if the committed JSON drifts from the generator.

## Kubernetes hardening checklist

- [x] Restricted Pod Security Admission on the namespace
- [x] UID 10001, `runAsNonRoot`, read-only root FS, `drop: [ALL]`, `RuntimeDefault` seccomp, no privilege escalation
- [x] Service-account tokens mounted only where needed (control)
- [x] Namespaced, read-only Role for control; no ClusterRole
- [x] Default-deny ingress NetworkPolicy plus explicit allows matching the call graph
- [x] Requests and limits on every container; `emptyDir` `/tmp` with a size limit
- [x] `maxUnavailable: 0` rolling updates, PDBs, topology spread across nodes
- [x] HPA owns `replicas` (the Deployment omits it when autoscaling, so upgrades don't fight the HPA)
- [x] Config checksum annotation, so ConfigMap changes roll the pods
- [x] Secrets generated on first install and preserved across upgrades (`lookup`)

## Known limits and next steps

- Redis is a single instance. For production, use ElastiCache or Redis Sentinel/Cluster. The queue design works unchanged on a replicated Redis.
- The HPA scales on CPU. Scaling task workers on **queue depth** (KEDA or Prometheus Adapter external metrics) would match the workload better.
- Alertmanager routing (Slack, PagerDuty) is left to the operator.
- There is no distributed tracing yet. The correlation ID is a stand-in; OpenTelemetry would be the next step.
