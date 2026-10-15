"""Redis-backed reliable job queue.

Layout
------
    autoheal:job:{id}          hash   job record
    autoheal:job:{id}:lease    string present while a live worker holds the job (TTL)
    autoheal:jobs:queue        list   pending ids (LPUSH in, consumed from the right)
    autoheal:jobs:processing   list   ids claimed by some worker
    autoheal:jobs:index        zset   ids by creation time (for listing)

Claiming uses BLMOVE queue -> processing, so a job is never only in a
worker's memory. If the worker's pod dies, its lease expires and any pod's
reaper moves the job back to the queue. LREM decides which reaper wins, so a
job is requeued exactly once. Delivery is therefore at-least-once; the
attempt counter doubles as a fencing token so a stale worker cannot
overwrite the outcome of a newer attempt.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from autoheal_common.schemas import Job, JobRequest, JobStatus

QUEUE = "autoheal:jobs:queue"
PROCESSING = "autoheal:jobs:processing"
INDEX = "autoheal:jobs:index"
INDEX_MAX = 2000


def job_key(job_id: str) -> str:
    return f"autoheal:job:{job_id}"


def lease_key(job_id: str) -> str:
    return f"autoheal:job:{job_id}:lease"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(raw: dict[str, str]) -> Job:
    return Job(
        id=raw["id"],
        type=raw["type"],
        params=json.loads(raw.get("params") or "{}"),
        status=JobStatus(raw["status"]),
        attempts=int(raw.get("attempts", 0)),
        max_attempts=int(raw.get("max_attempts", 3)),
        created_at=raw["created_at"],
        started_at=raw.get("started_at") or None,
        finished_at=raw.get("finished_at") or None,
        result=json.loads(raw["result"]) if raw.get("result") else None,
        error=raw.get("error") or None,
        worker=raw.get("worker") or None,
        correlation_id=raw.get("correlation_id") or None,
    )


class JobStore:
    def __init__(self, redis: Redis, *, lease_ttl: float, retention_seconds: int) -> None:
        self.redis = redis
        self.lease_ttl_ms = int(lease_ttl * 1000)
        self.retention_seconds = retention_seconds
        self._suspects: set[str] = set()

    async def ping(self) -> None:
        await self.redis.ping()

    # --- producer side -------------------------------------------------
    async def create(self, request: JobRequest, correlation_id: str | None) -> Job:
        job_id = str(uuid.uuid4())
        record = {
            "id": job_id,
            "type": request.type,
            "params": request.params.model_dump_json(),
            "status": JobStatus.QUEUED.value,
            "attempts": 0,
            "max_attempts": request.max_attempts,
            "created_at": _now(),
            "correlation_id": correlation_id or "",
        }
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(job_key(job_id), mapping=record)
            pipe.zadd(INDEX, {job_id: time.time()})
            pipe.zremrangebyrank(INDEX, 0, -(INDEX_MAX + 1))
            pipe.lpush(QUEUE, job_id)
            await pipe.execute()
        return _decode({k: str(v) for k, v in record.items()})

    async def get(self, job_id: str) -> Job | None:
        raw = await self.redis.hgetall(job_key(job_id))
        return _decode(raw) if raw else None

    async def list(self, limit: int, status: JobStatus | None = None) -> list[Job]:
        scan = limit if status is None else min(INDEX_MAX, limit * 10)
        ids = await self.redis.zrevrange(INDEX, 0, scan - 1)
        async with self.redis.pipeline(transaction=False) as pipe:
            for job_id in ids:
                pipe.hgetall(job_key(job_id))
            rows = await pipe.execute()
        jobs = [_decode(r) for r in rows if r]
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return jobs[:limit]

    async def depth(self) -> dict[str, int]:
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.llen(QUEUE)
            pipe.llen(PROCESSING)
            queued, processing = await pipe.execute()
        return {"queued": queued, "processing": processing}

    # --- consumer side -------------------------------------------------
    async def claim(self, worker: str, timeout: float) -> Job | None:
        job_id = await self.redis.blmove(QUEUE, PROCESSING, timeout, "RIGHT", "LEFT")
        if job_id is None:
            return None
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(lease_key(job_id), worker, px=self.lease_ttl_ms)
            pipe.hset(
                job_key(job_id),
                mapping={
                    "status": JobStatus.RUNNING.value,
                    "started_at": _now(),
                    "worker": worker,
                    "claimed_at": time.time(),
                },
            )
            pipe.hincrby(job_key(job_id), "attempts", 1)
            pipe.hgetall(job_key(job_id))
            *_, raw = await pipe.execute()
        if not raw.get("type"):  # record expired while queued; drop the orphan id
            await self.redis.delete(job_key(job_id), lease_key(job_id))
            await self.redis.lrem(PROCESSING, 1, job_id)
            return None
        return _decode(raw)

    async def renew_lease(self, job: Job, worker: str) -> None:
        await self.redis.set(lease_key(job.id), worker, px=self.lease_ttl_ms)

    async def _still_owned(self, job: Job) -> bool:
        attempts = await self.redis.hget(job_key(job.id), "attempts")
        return attempts is not None and int(attempts) == job.attempts

    async def complete(self, job: Job, result: dict[str, Any]) -> bool:
        if not await self._still_owned(job):
            return False
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                job_key(job.id),
                mapping={
                    "status": JobStatus.SUCCEEDED.value,
                    "finished_at": _now(),
                    "result": json.dumps(result),
                    "error": "",
                },
            )
            pipe.expire(job_key(job.id), self.retention_seconds)
            pipe.lrem(PROCESSING, 1, job.id)
            pipe.delete(lease_key(job.id))
            await pipe.execute()
        return True

    async def fail(self, job: Job, error: str) -> JobStatus:
        """Records a failed attempt. Returns QUEUED if it will be retried."""
        if not await self._still_owned(job):
            return JobStatus.RUNNING
        retry = job.attempts < job.max_attempts
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lrem(PROCESSING, 1, job.id)
            pipe.delete(lease_key(job.id))
            if retry:
                pipe.hset(job_key(job.id), mapping={"status": JobStatus.QUEUED.value, "error": error})
                pipe.lpush(QUEUE, job.id)
            else:
                pipe.hset(
                    job_key(job.id),
                    mapping={"status": JobStatus.FAILED.value, "error": error, "finished_at": _now()},
                )
                pipe.expire(job_key(job.id), self.retention_seconds)
            await pipe.execute()
        return JobStatus.QUEUED if retry else JobStatus.FAILED

    async def release(self, job: Job) -> bool:
        """Graceful shutdown: hand an unfinished job back without burning an attempt."""
        if await self.redis.lrem(PROCESSING, 1, job.id) != 1:
            return False
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(lease_key(job.id))
            pipe.hset(job_key(job.id), mapping={"status": JobStatus.QUEUED.value})
            pipe.hincrby(job_key(job.id), "attempts", -1)
            pipe.rpush(QUEUE, job.id)  # front of the line: it was already waiting
            await pipe.execute()
        return True

    async def reap_expired(self) -> list[str]:
        """Requeue jobs whose worker stopped renewing its lease. Safe to run
        concurrently on every pod: LREM lets exactly one reaper win per job."""
        ids: list[str] = await self.redis.lrange(PROCESSING, 0, -1)
        if not ids:
            self._suspects.clear()
            return []
        async with self.redis.pipeline(transaction=False) as pipe:
            for job_id in ids:
                pipe.exists(lease_key(job_id))
                pipe.hget(job_key(job_id), "claimed_at")
            flat = await pipe.execute()

        now = time.time()
        reaped: list[str] = []
        suspects: set[str] = set()
        for job_id, has_lease, claimed_at in zip(ids, flat[::2], flat[1::2], strict=True):
            if has_lease:
                continue
            if claimed_at is None:
                # Claimed but not yet stamped (worker died between BLMOVE and
                # HSET, or is about to stamp). Only act on a second sighting.
                if job_id not in self._suspects:
                    suspects.add(job_id)
                    continue
            elif now - float(claimed_at) < self.lease_ttl_ms / 1000:
                continue
            if await self.redis.lrem(PROCESSING, 1, job_id) != 1:
                continue  # another pod's reaper (or the worker itself) got there first
            raw = await self.redis.hgetall(job_key(job_id))
            if not raw.get("type"):
                continue
            attempts, max_attempts = int(raw.get("attempts", 0)), int(raw.get("max_attempts", 3))
            if attempts >= max_attempts:
                await self.redis.hset(
                    job_key(job_id),
                    mapping={
                        "status": JobStatus.FAILED.value,
                        "error": f"worker lost on final attempt {attempts}/{max_attempts}",
                        "finished_at": _now(),
                    },
                )
            else:
                async with self.redis.pipeline(transaction=True) as pipe:
                    pipe.hset(
                        job_key(job_id),
                        mapping={"status": JobStatus.QUEUED.value, "error": "worker lost; requeued"},
                    )
                    pipe.rpush(QUEUE, job_id)
                    await pipe.execute()
            reaped.append(job_id)
        self._suspects = suspects
        return reaped
