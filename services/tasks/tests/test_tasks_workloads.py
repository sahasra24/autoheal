import hashlib

import pytest

from autoheal_tasks.workloads import WorkloadError, hash_chain, prime_count, run_async_workload, run_cpu_workload


@pytest.mark.parametrize(("limit", "expected"), [(2, 0), (3, 1), (10, 4), (100, 25), (100_000, 9592)])
def test_prime_count(limit: int, expected: int) -> None:
    assert prime_count(limit)["primes"] == expected


def test_hash_chain_matches_manual_computation() -> None:
    digest = b"seed"
    for _ in range(3):
        digest = hashlib.sha256(digest).digest()
    assert hash_chain(3, "seed")["digest"] == digest.hex()


def test_cpu_dispatch_rejects_async_types() -> None:
    with pytest.raises(ValueError):
        run_cpu_workload("sleep", {"seconds": 0})


async def test_flaky_always_fails_at_rate_one() -> None:
    with pytest.raises(WorkloadError):
        await run_async_workload("flaky", {"failure_rate": 1.0, "seconds": 0})


async def test_flaky_never_fails_at_rate_zero() -> None:
    assert await run_async_workload("flaky", {"failure_rate": 0.0, "seconds": 0}) == {"survived": True}
