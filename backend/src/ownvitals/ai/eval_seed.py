"""The AI evaluation's synthetic seeder (ADR 0014 section 9; #531): accounts and their data in an
``_ai_eval`` database. Synthetic data only (``eval_seed_model``).

Two steps, each on the role its rows need:

- ``seed_accounts(owner_engine)``: one marked account per plan (``users.synthetic = true``),
  inserted on a connection of the **owner role**, the only role that may write the mark
  (migration 0031). ``ownvitals.auth.synthetic`` refuses a database whose name does not end in
  ``_ai_eval``, an address outside the reserved ``EVAL_EMAIL_DOMAIN``, and a database that
  holds an unmarked account. One transaction: all the plans' accounts or none.
- ``seed_account_data(factory, ctx, open_tx, settings, plan, now=...)``: the account's data, on
  the **app role** in ``ctx``'s own session (forced RLS), ``ctx`` being a context issued for that
  account (the evaluation runs its work as leased jobs, so the jobs issuer gives it) and
  ``open_tx`` that context's transaction opener (the job's fence): this module opens no session
  itself (``session_scan``). A
  credential-free test connection (``ownvitals.connections.synthetic``, which refuses anything
  but a marked account in an evaluation database), the plan's device, its weights and pulses through
  the real ingest, then the normalisation and daily passes, and consent to the current text.

Consent is recorded through ``consent.give``, which first asks whether the ``ai_insights``
switch is on; the evaluation serves no user and does not need that switch (ADR 0014 section 9, lock
1), so the seeder hands ``give`` a copy of the settings with it on. The copy never leaves this
function and gates nothing else. Both steps can run again: the account is reused, the rows are
upserts, and the same consent is kept.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ownvitals.ai import consent, consent_text
from ownvitals.ai.eval_seed_model import PLANS, ZONE, AccountPlan, device, measure_groups
from ownvitals.auth.synthetic import create_synthetic_account
from ownvitals.config import Settings
from ownvitals.connections.synthetic import create_synthetic_connection
from ownvitals.context import UserContext
from ownvitals.derived.daily_db import daily_owner
from ownvitals.derived.normalize_db import normalise_owner
from ownvitals.ingest.rows_measures import upsert_measure_group
from ownvitals.ingest.rows_streams import upsert_device

Factory = async_sessionmaker[AsyncSession]
OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]
CONSENT_LOCALE = "en"


async def seed_accounts(
    owner_engine: AsyncEngine, plans: tuple[AccountPlan, ...] = PLANS
) -> dict[str, UUID]:
    """Each plan's marked account id, by plan name (created or found again)."""
    async with owner_engine.begin() as conn:
        return {plan.name: await create_synthetic_account(conn, plan.email) for plan in plans}


async def seed_account_data(
    factory: Factory,
    ctx: UserContext,
    open_tx: OpenTx,
    settings: Settings,
    plan: AccountPlan,
    *,
    now: datetime,
) -> None:
    """Plant ``plan``'s data for the context's account (see the module docstring)."""
    index = PLANS.index(plan) if plan in PLANS else len(PLANS)
    connection = await create_synthetic_connection(factory, ctx)
    owner = ctx.user_id
    async with open_tx() as db:
        await upsert_device(db, owner, connection, device(plan), ZONE, now)
        for group in measure_groups(plan, index, now.date()):
            await upsert_measure_group(db, owner, connection, group, ZONE, now)
    await normalise_owner(open_tx, owner, now=now)
    await daily_owner(open_tx, owner)
    await consent.give(
        factory,
        ctx,
        settings.model_copy(update={"ai_insights": True}),
        version=consent_text.CONSENT_VERSION,
        locale=CONSENT_LOCALE,
        digest=consent_text.fingerprint(consent_text.TEXTS[CONSENT_LOCALE]),
    )
