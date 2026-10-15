from prometheus_client import Counter, Gauge, Histogram

JOBS_SUBMITTED = Counter("autoheal_jobs_submitted_total", "Jobs accepted.", ["type"])
JOBS_FINISHED = Counter(
    "autoheal_jobs_finished_total",
    "Job attempts that finished, by outcome (succeeded | failed | retried).",
    ["type", "outcome"],
)
JOBS_REQUEUED = Counter(
    "autoheal_jobs_requeued_total",
    "Jobs put back on the queue after their worker was lost or shut down.",
    ["reason"],
)
JOB_DURATION = Histogram(
    "autoheal_job_duration_seconds",
    "Wall-clock processing time per job attempt.",
    ["type"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
JOBS_IN_PROGRESS = Gauge("autoheal_jobs_in_progress", "Jobs being processed by this pod.")
QUEUE_DEPTH = Gauge("autoheal_job_queue_depth", "Jobs in the shared queue (same value on every pod).", ["state"])
COMPUTE_DURATION = Histogram(
    "autoheal_compute_duration_seconds",
    "Synchronous compute request duration.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
