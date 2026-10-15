from prometheus_client import Counter, Gauge, Histogram

RATE_LIMIT_DECISIONS = Counter(
    "autoheal_rate_limit_decisions_total",
    "Rate-limit decisions (allowed | rejected | bypassed when Redis is unavailable).",
    ["decision"],
)
AUTH_FAILURES = Counter("autoheal_auth_failures_total", "Requests rejected for a missing/invalid API key.")
UPSTREAM_REQUESTS = Counter(
    "autoheal_upstream_requests_total",
    "Calls from the gateway to backend services, by outcome.",
    ["upstream", "outcome"],
)
UPSTREAM_LATENCY = Histogram(
    "autoheal_upstream_request_duration_seconds",
    "Latency of gateway -> backend calls.",
    ["upstream"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CIRCUIT_STATE = Gauge(
    "autoheal_circuit_breaker_state",
    "Circuit breaker state per upstream: 0=closed, 1=half-open, 2=open.",
    ["upstream"],
)
