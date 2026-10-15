from __future__ import annotations

from pydantic import Field

from autoheal_common.config import BaseServiceSettings


class GatewaySettings(BaseServiceSettings):
    service_name: str = "gateway"

    tasks_url: str = "http://localhost:8001"
    control_url: str = "http://localhost:8002"
    redis_url: str = "redis://localhost:6379/0"

    # Sliding-window rate limit per client identity.
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    # When Redis is down, keep serving (degraded) rather than turn a cache
    # outage into a full API outage. Set false to fail closed.
    rate_limit_fail_open: bool = True

    # Comma-separated. Empty = auth disabled (local dev); the X-API-Key header,
    # if sent, is then only used as the rate-limit identity.
    api_keys: str = ""
    # Only trust X-Forwarded-For when running behind a known proxy/ingress.
    trust_forwarded_for: bool = False

    max_body_bytes: int = 64 * 1024
    upstream_timeout_seconds: float = 15.0
    upstream_connect_timeout_seconds: float = 2.0
    circuit_failure_threshold: int = Field(default=5, ge=1)
    circuit_reset_seconds: float = Field(default=15.0, gt=0)

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())
