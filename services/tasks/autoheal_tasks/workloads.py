"""Sample workloads.

CPU-bound functions are module-level and pure so they can be shipped to a
spawned worker process; I/O-style workloads are coroutines on the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
from typing import Any

CPU_BOUND = frozenset({"prime_count", "hash_chain"})


class WorkloadError(Exception):
    """A job failed in a way that is worth retrying."""


def prime_count(limit: int) -> dict[str, Any]:
    """Sieve of Eratosthenes - deliberately pure Python to burn CPU."""
    sieve = bytearray([1]) * limit
    sieve[0:2] = b"\x00\x00"
    for i in range(2, math.isqrt(limit - 1) + 1):
        if sieve[i]:
            sieve[i * i :: i] = bytes(len(range(i * i, limit, i)))
    return {"limit": limit, "primes": sum(sieve)}


def hash_chain(iterations: int, seed: str) -> dict[str, Any]:
    digest = seed.encode()
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return {"iterations": iterations, "digest": digest.hex()}


def run_cpu_workload(job_type: str, params: dict[str, Any]) -> dict[str, Any]:
    if job_type == "prime_count":
        return prime_count(**params)
    if job_type == "hash_chain":
        return hash_chain(**params)
    raise ValueError(f"not a CPU-bound workload: {job_type}")


async def run_async_workload(job_type: str, params: dict[str, Any]) -> dict[str, Any]:
    if job_type == "sleep":
        await asyncio.sleep(params["seconds"])
        return {"slept_seconds": params["seconds"]}
    if job_type == "flaky":
        await asyncio.sleep(params["seconds"])
        if random.random() < params["failure_rate"]:
            raise WorkloadError("simulated transient failure")
        return {"survived": True}
    raise ValueError(f"unknown workload: {job_type}")
