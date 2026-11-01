"""The admin area's Jobs view (classbeat ADR 0011 section 6; ownvitals #12).

``GET /api/admin/jobs?days=7`` (``job.list``), one read ``admin_session`` on ``ownvitals_maint``:

- **counts per state**, from ``jobs.state`` through maint's column ``SELECT`` (maint has no
  grant on ``queue_stats()``);
- **recent failures by category**: failed jobs that finished within ``days``, counted per kind
  and error category, with the latest.

Read only, and aggregates only: no owner, no id. Never a job's input, idempotency key or request
hash, nor any event payload: maint has no grant on those columns or on ``job_events``
(``tests/test_maint_privileges.py``) and nothing here asks. The per-user lists wait for the Users
view (#127).
"""

from datetime import datetime
from typing import Annotated

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ownvitals.admin.db import admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db

router = admin_router()

_STATES = text("SELECT state, count(*) AS n FROM jobs GROUP BY state ORDER BY state")
_FAILURES = text(
    "SELECT kind, error_category, count(*) AS n, max(finished_at) AS last_at FROM jobs "
    "WHERE state = 'failed' AND finished_at >= now() - make_interval(days => :days) "
    "GROUP BY kind, error_category ORDER BY n DESC, kind, error_category"
)


class Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class StateCount(Out):
    state: str
    count: int


class Failure(Out):
    kind: str
    category: str
    count: int
    last_at: datetime


class JobsOut(Out):
    days: int
    states: list[StateCount]
    failures: list[Failure]


@router.get("/jobs", response_model=JobsOut)
async def read_jobs(
    request: Request, actor: AdminActor, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> JobsOut:
    async with admin_session(require_db(request), actor, "job.list", params={"days": days}) as db:
        states = (await db.execute(_STATES)).all()
        failures = (await db.execute(_FAILURES, {"days": days})).all()
    return JobsOut(
        days=days,
        states=[StateCount(state=r.state, count=int(r.n)) for r in states],
        failures=[
            Failure(kind=r.kind, category=r.error_category, count=int(r.n), last_at=r.last_at)
            for r in failures
        ],
    )
