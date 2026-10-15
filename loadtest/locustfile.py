"""Load profile for AutoHeal.

    pip install -r loadtest/requirements.txt
    locust -f loadtest/locustfile.py --host http://localhost:8080            # web UI on :8089
    locust -f loadtest/locustfile.py --host http://localhost:8080 --headless \
           --users 60 --spawn-rate 5 --run-time 10m --html loadtest/reports/run.html

Set AUTOHEAL_STEP_LOAD=1 to use the stepped shape below, which is designed to
push the task tier past its HPA CPU target, hold, then drop so you can watch
it scale out and back in on the dashboard.

Each simulated user sends its own X-API-Key, so the per-client rate limit
applies per user rather than throttling the whole test as one IP.
"""

from __future__ import annotations

import itertools
import os
import random
import uuid

from locust import FastHttpUser, LoadTestShape, between, events, task

_user_ids = itertools.count()


class PlatformUser(FastHttpUser):
    wait_time = between(0.5, 2)

    def on_start(self) -> None:
        key = os.getenv("AUTOHEAL_API_KEY") or f"loadtest-{next(_user_ids)}"
        self.headers = {"X-API-Key": key}
        self.job_ids: list[str] = []

    def _h(self) -> dict[str, str]:
        return {**self.headers, "X-Correlation-ID": f"locust-{uuid.uuid4().hex[:16]}"}

    @task(5)
    def compute(self) -> None:
        """Synchronous CPU work: the main lever on the HPA."""
        with self.client.post(
            "/api/v1/compute",
            json={"iterations": random.randint(50_000, 150_000)},
            headers=self._h(),
            name="/api/v1/compute",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                resp.success()  # rate limiting is expected behaviour, not a failure

    @task(3)
    def submit_job(self) -> None:
        job = random.choice(
            [
                {"type": "prime_count", "params": {"limit": random.randint(100_000, 800_000)}},
                {"type": "hash_chain", "params": {"iterations": random.randint(50_000, 300_000)}},
                {"type": "sleep", "params": {"seconds": round(random.uniform(0.1, 1.5), 2)}},
                {"type": "flaky", "params": {"failure_rate": 0.3, "seconds": 0.2}},
            ]
        )
        with self.client.post("/api/v1/jobs", json=job, headers=self._h(), catch_response=True) as resp:
            if resp.status_code == 202:
                self.job_ids = [*self.job_ids, resp.json()["id"]][-20:]
            elif resp.status_code == 429:
                resp.success()

    @task(3)
    def poll_job(self) -> None:
        if self.job_ids:
            self.client.get(f"/api/v1/jobs/{random.choice(self.job_ids)}", headers=self._h(), name="/api/v1/jobs/[id]")

    @task(1)
    def list_jobs(self) -> None:
        self.client.get("/api/v1/jobs?limit=20", headers=self._h(), name="/api/v1/jobs")

    @task(1)
    def queue(self) -> None:
        self.client.get("/api/v1/queue", headers=self._h())


class StepLoad(LoadTestShape):
    """Warm-up -> ramp -> sustained peak -> cool-down (total ~12 min)."""

    stages = [
        (60, 5),  # until t=60s: 5 users
        (180, 20),
        (300, 40),
        (540, 60),  # sustained peak: HPA should be scaling out
        (720, 5),  # cool-down: watch scale-in after the stabilisation window
    ]

    def tick(self) -> tuple[int, float] | None:
        t = self.get_run_time()
        for until, users in self.stages:
            if t < until:
                return users, 5
        return None


if not os.getenv("AUTOHEAL_STEP_LOAD"):
    del StepLoad  # only activate the shape when asked


@events.quitting.add_listener
def _fail_on_errors(environment, **_kw) -> None:
    """Non-zero exit in CI when the platform misbehaves under load."""
    stats = environment.stats.total
    if stats.num_requests and stats.fail_ratio > 0.02:
        environment.process_exit_code = 1
