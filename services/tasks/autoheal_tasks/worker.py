"""In-pod worker pool: N queue consumers, a lease reaper and a queue-depth sampler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context

from redis.exceptions import RedisError

from autoheal_common.health import HealthState
from autoheal_common.logging import correlation_id_var
from autoheal_common.schemas import Job, JobStatus
from autoheal_tasks.config import TaskSettings
from autoheal_tasks.metrics import (
    JOB_DURATION,
    JOBS_FINISHED,
    JOBS_IN_PROGRESS,
    JOBS_REQUEUED,
    QUEUE_DEPTH,
)
from autoheal_tasks.store import JobStore
from autoheal_tasks.workloads import CPU_BOUND, run_async_workload, run_cpu_workload

log = logging.getLogger(__name__)


def make_executor(kind: str, workers: int) -> Executor:
    if kind == "process":
        return ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"))
    return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="workload")


class WorkerPool:
    def __init__(self, store: JobStore, settings: TaskSettings, executor: Executor, health: HealthState) -> None:
        self.store = store
        self.settings = settings
        self.executor = executor
        self.health = health
        self.worker_id = settings.pod_name
        self._stopping = asyncio.Event()
        self._consumers: list[asyncio.Task[None]] = []
        self._background: list[asyncio.Task[None]] = []
        self._inflight: dict[str, Job] = {}

    async def start(self) -> None:
        for i in range(self.settings.worker_concurrency):
            self._consumers.append(asyncio.create_task(self._consume(), name=f"consumer-{i}"))
        self._background.append(asyncio.create_task(self._reaper(), name="reaper"))
        self._background.append(asyncio.create_task(self._sample_depth(), name="depth"))
        log.info("worker pool started", extra={"consumers": len(self._consumers)})

    async def stop(self, timeout: float) -> None:
        """Stop claiming, give in-flight jobs `timeout` seconds, then hand the
        rest back to the queue so another pod picks them up immediately."""
        self._stopping.set()
        for task in self._background:
            task.cancel()
        _, pending = await asyncio.wait(self._consumers, timeout=timeout) if self._consumers else (set(), set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*self._consumers, *self._background, return_exceptions=True)
        for job in list(self._inflight.values()):
            try:
                if await self.store.release(job):
                    JOBS_REQUEUED.labels("shutdown").inc()
                    log.info("released in-flight job on shutdown", extra={"job_id": job.id})
            except RedisError:
                log.warning("could not release job; its lease will expire", extra={"job_id": job.id})
        log.info("worker pool stopped")

    # --- loops ----------------------------------------------------------
    async def _consume(self) -> None:
        backoff = 0.5
        poll = self.settings.queue_poll_timeout_seconds
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                job = await self.store.claim(self.worker_id, poll)
                backoff = 0.5
            except RedisError as exc:
                log.warning("queue unavailable, backing off", extra={"error": str(exc), "backoff_s": backoff})
                await self._sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            if job is not None:
                await self._process(job)
            elif (waited := time.monotonic() - started) < poll:
                # The blocking pop returned early (proxy/backends that don't
                # honour BLMOVE timeouts); never spin the event loop.
                await self._sleep(poll - waited)

    async def _process(self, job: Job) -> None:
        token = correlation_id_var.set(job.correlation_id)
        self._inflight[job.id] = job
        JOBS_IN_PROGRESS.inc()
        heartbeat = asyncio.create_task(self._heartbeat(job))
        start = time.perf_counter()
        cancelled = False
        log.info("job started", extra={"job_id": job.id, "type": job.type, "attempt": job.attempts})
        try:
            if job.type in CPU_BOUND:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(self.executor, run_cpu_workload, job.type, job.params)
            else:
                result = await run_async_workload(job.type, job.params)
        except asyncio.CancelledError:
            cancelled = True  # shutdown: stop() releases the job back to the queue
            raise
        except BrokenProcessPool:
            # A workload process was killed (e.g. OOM). The pool cannot recover
            # in-process, so fail liveness and let Kubernetes restart us cleanly.
            self.health.mark_unhealthy("workload process pool is broken")
            await self._record_failure(job, "worker process crashed", start)
        except Exception as exc:  # noqa: BLE001 - workload errors are job outcomes
            await self._record_failure(job, f"{type(exc).__name__}: {exc}", start)
        else:
            JOB_DURATION.labels(job.type).observe(time.perf_counter() - start)
            if await self.store.complete(job, result):
                JOBS_FINISHED.labels(job.type, "succeeded").inc()
                log.info("job succeeded", extra={"job_id": job.id, "duration_s": round(time.perf_counter() - start, 3)})
            else:
                log.warning("job outcome discarded: a newer attempt owns it", extra={"job_id": job.id})
        finally:
            heartbeat.cancel()
            JOBS_IN_PROGRESS.dec()
            correlation_id_var.reset(token)
            if not cancelled:
                self._inflight.pop(job.id, None)

    async def _record_failure(self, job: Job, error: str, start: float) -> None:
        JOB_DURATION.labels(job.type).observe(time.perf_counter() - start)
        outcome = await self.store.fail(job, error)
        label = "retried" if outcome == JobStatus.QUEUED else "failed"
        JOBS_FINISHED.labels(job.type, label).inc()
        log.warning("job attempt failed", extra={"job_id": job.id, "error": error, "outcome": label})

    async def _heartbeat(self, job: Job) -> None:
        interval = self.settings.lease_ttl_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                await self.store.renew_lease(job, self.worker_id)
            except RedisError as exc:
                log.warning("lease renewal failed", extra={"job_id": job.id, "error": str(exc)})

    async def _reaper(self) -> None:
        while not self._stopping.is_set():
            try:
                for job_id in await self.store.reap_expired():
                    JOBS_REQUEUED.labels("lease_expired").inc()
                    log.warning("recovered job from a lost worker", extra={"job_id": job_id})
            except RedisError as exc:
                log.debug("reaper skipped", extra={"error": str(exc)})
            await self._sleep(self.settings.reaper_interval_seconds)

    async def _sample_depth(self) -> None:
        while not self._stopping.is_set():
            try:
                for state, value in (await self.store.depth()).items():
                    QUEUE_DEPTH.labels(state).set(value)
            except RedisError:
                pass
            await self._sleep(5)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
