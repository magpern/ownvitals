"""The admin Overview's tiles (issue 549): ``GET /api/admin/overview`` (``overview.read``).

One audited read ``admin_session`` on ``ownvitals_maint``, counts only:

- **accounts**: how many, and how many have a confirmed authenticator (``ownvitals.auth``'s
  ``account_counts``, its own ``system_session`` on the app role: maint has no grant on the
  account tables), with the sign-up mode in force (``ownvitals.flags``, the stricter of the
  server setting and the stored flag, which maint may read);
- **connections**: accounts per Withings state (``healthy``, ``needs_reconnect``,
  ``not_connected``), through the definer ``admin_withings_counts``
  (``ownvitals.admin.connection_states``), which refuses without this transaction's ``ok`` row;
- **jobs**: how many are ``queued``, ``running`` and ``dead_letter``, through maint's column
  grant on ``jobs.state``, as the Jobs view counts them.

No id, address, owner, kind, input or health value. The counts come from two transactions (the
account tables' and maint's), so a change between them can show for one request.
"""

from typing import Final

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.admin.connection_states import withings_counts
from ownvitals.admin.db import admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db
from ownvitals.auth.admin_counts import account_counts
from ownvitals.config import Settings
from ownvitals.flags import effective
from ownvitals.flags.core import SignupMode

router = admin_router()

JOB_STATES: Final = ("queued", "running", "dead_letter")
_JOBS = text(
    "SELECT state, count(*) AS n FROM jobs WHERE state = ANY(:states) GROUP BY state ORDER BY state"
)


class Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class AccountTile(Out):
    total: int
    second_factor_on: int
    signup: SignupMode


class ConnectionTile(Out):
    healthy: int
    needs_reconnect: int
    not_connected: int


class JobTile(Out):
    queued: int
    running: int
    dead_letter: int


class OverviewOut(Out):
    accounts: AccountTile
    connections: ConnectionTile
    jobs: JobTile


@router.get("/overview", response_model=OverviewOut)
async def read_overview(request: Request, actor: AdminActor) -> OverviewOut:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    settings: Settings = request.app.state.settings
    async with admin_session(require_db(request), actor, "overview.read") as db:
        signup = await effective.signup_mode(db, settings.signup_mode)
        connections = await withings_counts(db)
        rows = (await db.execute(_JOBS, {"states": list(JOB_STATES)})).all()
        accounts = await account_counts(factory)
    jobs = {state: 0 for state in JOB_STATES} | {r.state: int(r.n) for r in rows}
    return OverviewOut(
        accounts=AccountTile(
            total=accounts.accounts, second_factor_on=accounts.second_factor_on, signup=signup
        ),
        connections=ConnectionTile(**connections),
        jobs=JobTile(**jobs),
    )
