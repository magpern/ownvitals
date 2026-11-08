"""The jobs and worker settings of ``ownvitals.config`` (fields only)."""

from pydantic import BaseModel, Field


class JobSettings(BaseModel):
    """Worker, lease and job-limit fields. Their bounds are the ``Field`` constraints."""

    # Jobs and the worker.
    # The worker's claim poll when no ``jobs_queued`` notification arrives.
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = Field(default=120, ge=30, le=600)
    # The worker's engine pool follows it.
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    # Reap, expire and settle every this many seconds.
    worker_maintenance_seconds: float = Field(default=30.0, gt=0)
    # On SIGTERM, how long runs may take to reach a checkpoint or return from a call before their
    # leases are handed back (inside compose's 30 s stop_grace_period).
    worker_shutdown_wait_seconds: float = Field(default=25.0, ge=0, le=25)
    # Queued and running jobs per user; creation above it is a 429.
    jobs_per_user: int = Field(default=2, ge=1, le=100)
    # ``operation_busy`` requeues per phase that cost no attempt; after that each one counts.
    job_busy_requeue_limit: int = Field(default=5, ge=0, le=100)
