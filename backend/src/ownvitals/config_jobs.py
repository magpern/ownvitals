"""The jobs and worker settings of ``ownvitals.config`` (fields only)."""

from pydantic import Field
from pydantic_settings import BaseSettings


class JobSettings(BaseSettings):
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
    # The worker's /metrics listener (#167): a loopback or private IP literal to bind (checked by
    # ownvitals.metrics_server at start), or None for no listener. Never routed through SWAG.
    metrics_bind: str | None = None
    metrics_port: int = Field(default=9464, ge=1, le=65535)
    # The database-derived gauges are refreshed every this many seconds.
    metrics_refresh_seconds: float = Field(default=30.0, ge=5, le=3600)
    # The JSON/CSV export (ADR 0013 section 4; #211): the archive's size limit (over it the build
    # fails ``export_too_large`` and keeps nothing) and how long a ready archive can be
    # downloaded. FIT output (``export_fit``, ADR 0013 section 3) is not built yet.
    export_max_bytes: int = Field(default=1 << 30, ge=1024, le=1 << 34)
    export_ttl_hours: int = Field(default=24, ge=1, le=168)
