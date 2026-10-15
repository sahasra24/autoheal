"""Distributed sliding-window-log rate limiter on Redis.

Every gateway replica shares the same Redis sorted set per client, so the
limit holds no matter which pod a request lands on. Each request is a member
scored by its timestamp; members older than the window are trimmed, and the
remaining cardinality is the request count in the trailing window. This
avoids the burst-at-the-boundary problem of fixed windows.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from autoheal_gateway.metrics import RATE_LIMIT_DECISIONS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after: float  # seconds until a slot frees up
    degraded: bool = False


class SlidingWindowRateLimiter:
    def __init__(self, redis: Redis, *, limit: int, window_seconds: int, fail_open: bool = True) -> None:
        self.redis = redis
        self.limit = limit
        self.window_ms = window_seconds * 1000
        self.fail_open = fail_open
        self._last_warning = 0.0

    async def hit(self, identity: str) -> RateLimitResult:
        key = f"autoheal:ratelimit:{identity}"
        now_ms = time.time() * 1000
        member = f"{now_ms:.3f}:{uuid.uuid4().hex[:8]}"
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now_ms - self.window_ms)
                pipe.zcard(key)
                pipe.zadd(key, {member: now_ms})
                pipe.pexpire(key, self.window_ms)
                _, count, _, _ = await pipe.execute()

            if count >= self.limit:
                # Rejected requests don't consume quota.
                await self.redis.zrem(key, member)
                oldest = await self.redis.zrange(key, 0, 0, withscores=True)
                reset_after = ((oldest[0][1] + self.window_ms) - now_ms) / 1000 if oldest else self.window_ms / 1000
                RATE_LIMIT_DECISIONS.labels("rejected").inc()
                return RateLimitResult(False, self.limit, 0, max(reset_after, 0.0))

            RATE_LIMIT_DECISIONS.labels("allowed").inc()
            return RateLimitResult(True, self.limit, self.limit - count - 1, self.window_ms / 1000)
        except RedisError as exc:
            return self._degraded(exc)

    def _degraded(self, exc: RedisError) -> RateLimitResult:
        now = time.monotonic()
        if now - self._last_warning > 10:  # don't flood logs during an outage
            log.warning(
                "rate limiter backend unavailable",
                extra={"error": f"{type(exc).__name__}: {exc}", "fail_open": self.fail_open},
            )
            self._last_warning = now
        if self.fail_open:
            RATE_LIMIT_DECISIONS.labels("bypassed").inc()
            return RateLimitResult(True, self.limit, self.limit, self.window_ms / 1000, degraded=True)
        RATE_LIMIT_DECISIONS.labels("rejected").inc()
        return RateLimitResult(False, self.limit, 0, 1.0, degraded=True)
