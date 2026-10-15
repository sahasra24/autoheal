#!/usr/bin/env bash
# End-to-end check against a running cluster with the chart installed:
#   1. all deployments roll out
#   2. a job submitted through the gateway completes
#   3. crash, liveness-failure and pod-deletion scenarios self-heal in time
# Used by CI (kind) and runnable locally: ./scripts/e2e.sh
set -euo pipefail

NS="${NS:-autoheal}"
REL="${REL:-autoheal}"
TIMEOUT="${TIMEOUT:-180}"
GW_PORT=18080
CTL_PORT=18090

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

cleanup() {
  [[ -n "${PF_GW:-}" ]] && kill "$PF_GW" 2>/dev/null || true
  [[ -n "${PF_CTL:-}" ]] && kill "$PF_CTL" 2>/dev/null || true
}
trap cleanup EXIT

on_failure() {
  log "FAILED - cluster state for debugging"
  kubectl -n "$NS" get pods -o wide || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -40 || true
  kubectl -n "$NS" logs -l app.kubernetes.io/component=tasks --tail=50 --all-containers || true
}
trap on_failure ERR

log "Waiting for rollouts"
for d in redis gateway tasks control; do
  kubectl -n "$NS" rollout status "deploy/${REL}-${d}" --timeout="${TIMEOUT}s"
done

log "Port-forwarding gateway and control"
kubectl -n "$NS" port-forward "svc/${REL}-gateway" "${GW_PORT}:80" >/dev/null 2>&1 & PF_GW=$!
kubectl -n "$NS" port-forward "svc/${REL}-control" "${CTL_PORT}:80" >/dev/null 2>&1 & PF_CTL=$!
for _ in $(seq 1 30); do
  curl -fsS "localhost:${GW_PORT}/health/ready" >/dev/null 2>&1 && curl -fsS "localhost:${CTL_PORT}/health/live" >/dev/null 2>&1 && break
  sleep 1
done

log "Smoke test: submit a job through the gateway"
JOB_ID=$(curl -fsS -X POST "localhost:${GW_PORT}/api/v1/jobs" \
  -H 'Content-Type: application/json' -H 'X-Correlation-ID: e2e-smoke-0001' \
  -d '{"type":"prime_count","params":{"limit":100000}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "job ${JOB_ID}"
for _ in $(seq 1 60); do
  STATUS=$(curl -fsS "localhost:${GW_PORT}/api/v1/jobs/${JOB_ID}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [[ "$STATUS" == "succeeded" ]] && break
  sleep 1
done
[[ "$STATUS" == "succeeded" ]] || { echo "job did not succeed (status=$STATUS)"; exit 1; }
curl -fsS "localhost:${GW_PORT}/api/v1/jobs/${JOB_ID}" | python3 -c 'import json,sys; j=json.load(sys.stdin); assert j["result"]["primes"]==9592, j; print("result ok:", j["result"])'

log "Self-healing scenarios"
export CHAOS_TOKEN
CHAOS_TOKEN=$(kubectl -n "$NS" get secret "${REL}-secrets" -o jsonpath='{.data.chaos-token}' | base64 -d)
for scenario in crash unhealthy pod_delete; do
  python3 scripts/chaos_demo.py "$scenario" --control "http://localhost:${CTL_PORT}" --timeout "$TIMEOUT"
done

log "Restart counts after the experiments"
kubectl -n "$NS" get pods -l app.kubernetes.io/component=tasks \
  -o custom-columns=POD:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount

log "E2E passed"
