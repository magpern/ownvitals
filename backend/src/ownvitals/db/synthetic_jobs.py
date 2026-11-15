"""The AI evaluation's database checks and its prepared runs (ADR 0014 section 9; #527).

The evaluation runs every model call inside a leased running job (ADR 0005), but nothing can
queue a job for an account that has neither a ``UserContext`` (only the issuers make one) nor a
live connection (``queue_enqueue``; the synthetic connection is ``disconnected``). So the
owner's **prepare** step (``python -m ownvitals ai-evaluation prepare``, on the owner role's
URL, never in the evaluation's own process) queues one run per seeded account here:
``queue_run(owner, user_id, plan)`` inserts a ``queued`` job of ``EVAL_RUN_KIND`` (origin
``operator``, one attempt, input ``{"plan", "revision"}``) and its ``queued`` event, in the
owner's transaction. The tables' RLS is forced, so the insert runs with ``app.user_id`` set to
that account, transaction-locally, and reset before the function returns. It refuses
(``SyntheticJobRefused``, nothing written), each check fail-closed, unless:

- the connected database's own name ends in ``_ai_eval`` (``not_an_evaluation_database``);
- the cluster holds no other database but ``postgres``, the templates and other ``_ai_eval``
  ones (``shared_cluster``): the app's database is never beside it, so the app role's password,
  which is cluster-wide, cannot open real data from here. This replaces comparing
  ``pg_control_system().system_identifier`` with the app database's, which would need the
  app's URL in the evaluation's process;
- the schema is at ``SCHEMA_HEAD`` (``schema_not_at_head``): ``alembic_version``, which only the
  owner role may read; the evaluation's job refuses an input whose ``revision`` is not it;
- the account is marked ``users.synthetic`` (``unmarked_account``).

``database_refusal(factory)`` is the evaluation process's own start check, as the app role in a
``system_session``: the same database and cluster checks and no unmarked account at all.
"""

import json
import re
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ownvitals.auth.synthetic import account_is_synthetic, is_eval_database_name, unmarked_accounts
from ownvitals.db.session import system_session

# The newest migration's revision. ``tests/test_ai_eval_jobs_db.py`` pins it to Alembic's head:
# a new migration fails that test until this names it.
SCHEMA_HEAD: Final = "0031_users_synthetic"
EVAL_RUN_KIND: Final = "ai_eval_run"
SYSTEM_DATABASES: Final = frozenset({"postgres", "template0", "template1"})
PLAN_NAME: Final = re.compile(r"^[a-z0-9-]{1,40}$")

_DATABASE = text("SELECT current_database()")
_DATABASES = text("SELECT datname FROM pg_catalog.pg_database")
_HAS_VERSIONS = text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
_VERSIONS = text("SELECT version_num FROM public.alembic_version")
_AS_ACCOUNT = text("SELECT set_config('app.user_id', :user_id, true)")
_QUEUED = text(
    "SELECT id FROM public.jobs WHERE owner_id = :o AND kind = :k AND state = 'queued' "
    "AND input = CAST(:input AS jsonb)"
)
_JOB = text(
    "INSERT INTO public.jobs (owner_id, kind, origin, lane, state, input, max_attempts, "
    "next_seq) VALUES (:o, :k, 'operator', 3, 'queued', CAST(:input AS jsonb), 1, 1) "
    "RETURNING id"
)
_EVENT = text(
    "INSERT INTO public.job_events (owner_id, job_id, seq, kind, payload, lease_version) "
    "VALUES (:o, :j, 1, 'queued', '{}'::jsonb, 0)"
)


class SyntheticJobRefused(Exception):
    """The database, schema or account is not one a run may be queued for; nothing written.
    ``code`` is a closed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def cluster_refusal(conn: AsyncConnection | AsyncSession) -> str | None:
    """``None`` when this database is an evaluation database alone in its cluster."""
    if not is_eval_database_name(str((await conn.execute(_DATABASE)).scalar_one())):
        return "not_an_evaluation_database"
    for name in (await conn.execute(_DATABASES)).scalars():
        if str(name) not in SYSTEM_DATABASES and not is_eval_database_name(str(name)):
            return "shared_cluster"
    return None


async def schema_refusal(owner: AsyncConnection) -> str | None:
    """``None`` when ``alembic_version`` holds exactly ``SCHEMA_HEAD`` (owner role only)."""
    if not (await owner.execute(_HAS_VERSIONS)).scalar_one():
        return "schema_not_at_head"
    versions = [str(v) for v in (await owner.execute(_VERSIONS)).scalars()]
    return None if versions == [SCHEMA_HEAD] else "schema_not_at_head"


async def database_refusal(factory: async_sessionmaker[AsyncSession]) -> str | None:
    """The evaluation's start check, as the app role: the database, its cluster, no account
    that is not marked synthetic."""
    async with system_session(factory) as db:
        refused = await cluster_refusal(db)
        if refused is None and await unmarked_accounts(db):
            refused = "unmarked_account"
    return refused


async def queue_run(owner: AsyncConnection, user_id: UUID, plan: str) -> UUID:
    """Queue one run of ``plan`` for the marked account ``user_id`` (or find the queued one), in
    the owner role's transaction the caller commits. See the module docstring."""
    if type(user_id) is not UUID or not PLAN_NAME.match(plan):
        raise SyntheticJobRefused("invalid_run")
    refused = await cluster_refusal(owner) or await schema_refusal(owner)
    if refused is not None:
        raise SyntheticJobRefused(refused)
    if not await account_is_synthetic(owner, user_id):
        raise SyntheticJobRefused("unmarked_account")
    payload = json.dumps({"plan": plan, "revision": SCHEMA_HEAD}, sort_keys=True)
    params = {"o": user_id, "k": EVAL_RUN_KIND, "input": payload}
    # Transaction-local (third argument true); a failure below rolls it back with the rest.
    await owner.execute(_AS_ACCOUNT, {"user_id": str(user_id)})
    job_id = (await owner.execute(_QUEUED, params)).scalar()
    if job_id is None:
        job_id = (await owner.execute(_JOB, params)).scalar_one()
        await owner.execute(_EVENT, {"o": user_id, "j": job_id})
    await owner.execute(_AS_ACCOUNT, {"user_id": ""})  # the rest of the transaction: no account
    return job_id
