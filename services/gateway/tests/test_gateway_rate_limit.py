from __future__ import annotations

from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis

from autoheal_gateway.rate_limit import SlidingWindowRateLimiter


def _limiter(redis: Redis, **kw: object) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(redis, limit=3, window_seconds=60, **kw)


async def test_allows_up_to_limit_then_rejects() -> None:
    limiter = _limiter(FakeAsyncRedis(decode_responses=True))
    results = [await limiter.hit("ip:1.2.3.4") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert 0 < results[-1].reset_after <= 60


async def test_rejected_requests_do_not_consume_quota() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    limiter = _limiter(redis)
    for _ in range(10):
        await limiter.hit("ip:a")
    assert await redis.zcard("autoheal:ratelimit:ip:a") == 3


async def test_identities_are_isolated() -> None:
    limiter = _limiter(FakeAsyncRedis(decode_responses=True))
    for _ in range(3):
        await limiter.hit("ip:a")
    assert (await limiter.hit("ip:b")).allowed


async def test_limit_is_shared_across_gateway_replicas() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    pod_a, pod_b = _limiter(redis), _limiter(redis)
    await pod_a.hit("ip:a")
    await pod_b.hit("ip:a")
    await pod_a.hit("ip:a")
    assert not (await pod_b.hit("ip:a")).allowed


def _unreachable_redis() -> Redis:
    return Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)


async def test_fails_open_when_redis_is_down() -> None:
    result = await _limiter(_unreachable_redis(), fail_open=True).hit("ip:a")
    assert result.allowed and result.degraded


async def test_fails_closed_when_configured() -> None:
    result = await _limiter(_unreachable_redis(), fail_open=False).hit("ip:a")
    assert not result.allowed and result.degraded
