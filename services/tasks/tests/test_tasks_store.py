"""Reliable-queue semantics, exercised directly against an in-memory Redis."""

from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from autoheal_common.schemas import HashChainJob, JobStatus, SleepJob
from autoheal_tasks.store import PROCESSING, QUEUE, JobStore


@pytest.fixture
async def store() -> JobStore:
    return JobStore(FakeAsyncRedis(decode_responses=True), lease_ttl=0.2, retention_seconds=60)


async def test_create_enqueues_and_indexes(store: JobStore) -> None:
    job = await store.create(HashChainJob(type="hash_chain"), "cid-12345678")
    assert job.status == JobStatus.QUEUED
    assert await store.depth() == {"queued": 1, "processing": 0}
    fetched = await store.get(job.id)
    assert fetched is not None and fetched.correlation_id == "cid-12345678"
    assert [j.id for j in await store.list(10)] == [job.id]


async def test_claim_moves_job_to_processing_with_lease(store: JobStore) -> None:
    job = await store.create(SleepJob(type="sleep"), None)
    claimed = await store.claim("worker-a", timeout=0.1)
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == JobStatus.RUNNING and claimed.attempts == 1 and claimed.worker == "worker-a"
    assert await store.depth() == {"queued": 0, "processing": 1}


async def test_claim_returns_none_when_queue_empty(store: JobStore) -> None:
    assert await store.claim("worker-a", timeout=0.05) is None


async def test_complete_clears_processing(store: JobStore) -> None:
    await store.create(SleepJob(type="sleep"), None)
    job = await store.claim("w", 0.1)
    assert await store.complete(job, {"ok": True})
    done = await store.get(job.id)
    assert done.status == JobStatus.SUCCEEDED and done.result == {"ok": True}
    assert await store.depth() == {"queued": 0, "processing": 0}


async def test_failure_retries_until_max_attempts(store: JobStore) -> None:
    await store.create(SleepJob(type="sleep", max_attempts=2), None)
    first = await store.claim("w", 0.1)
    assert await store.fail(first, "boom") == JobStatus.QUEUED
    second = await store.claim("w", 0.1)
    assert second.attempts == 2
    assert await store.fail(second, "boom again") == JobStatus.FAILED
    final = await store.get(second.id)
    assert final.status == JobStatus.FAILED and final.error == "boom again"
    assert await store.depth() == {"queued": 0, "processing": 0}


async def test_reaper_requeues_job_of_dead_worker(store: JobStore) -> None:
    """Scenario: the pod running a job is killed. Its lease lapses and any
    surviving pod's reaper puts the job back on the queue."""
    await store.create(SleepJob(type="sleep"), None)
    orphan = await store.claim("doomed-pod", 0.1)
    assert await store.reap_expired() == []  # lease still valid
    await asyncio.sleep(0.3)  # lease (0.2s) expires; no heartbeat
    assert await store.reap_expired() == [orphan.id]
    assert (await store.get(orphan.id)).status == JobStatus.QUEUED
    survivor = await store.claim("healthy-pod", 0.1)
    assert survivor.id == orphan.id and survivor.attempts == 2


async def test_concurrent_reapers_requeue_exactly_once(store: JobStore) -> None:
    await store.create(SleepJob(type="sleep"), None)
    await store.claim("doomed-pod", 0.1)
    await asyncio.sleep(0.3)
    other = JobStore(store.redis, lease_ttl=0.2, retention_seconds=60)
    results = await asyncio.gather(store.reap_expired(), other.reap_expired())
    assert sum(len(r) for r in results) == 1
    assert await store.redis.llen(QUEUE) == 1
    assert await store.redis.llen(PROCESSING) == 0


async def test_lost_job_on_final_attempt_is_failed(store: JobStore) -> None:
    await store.create(SleepJob(type="sleep", max_attempts=1), None)
    job = await store.claim("doomed-pod", 0.1)
    await asyncio.sleep(0.3)
    await store.reap_expired()
    final = await store.get(job.id)
    assert final.status == JobStatus.FAILED and "worker lost" in final.error


async def test_stale_worker_cannot_overwrite_newer_attempt(store: JobStore) -> None:
    await store.create(SleepJob(type="sleep"), None)
    stale = await store.claim("slow-pod", 0.1)
    await asyncio.sleep(0.3)
    await store.reap_expired()
    fresh = await store.claim("fast-pod", 0.1)
    assert not await store.complete(stale, {"from": "stale"})
    assert await store.complete(fresh, {"from": "fresh"})
    assert (await store.get(fresh.id)).result == {"from": "fresh"}


async def test_release_returns_job_without_consuming_attempt(store: JobStore) -> None:
    await store.create(SleepJob(type="sleep"), None)
    job = await store.claim("w", 0.1)
    assert await store.release(job)
    again = await store.claim("w2", 0.1)
    assert again.id == job.id and again.attempts == 1
