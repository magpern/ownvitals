"""The "also delete imported data" option of a disconnect (PLAN section 4.3, ADR 0004 section 7).

It runs after the disconnect cleared the credentials, in the user's own session. Imported rows
first, with the analysis rows derived from them (#218); the user's settings stay. Then this
owner's webhook events through their definer (it finds them through the connection rows, so
before the row goes), then the disconnected row itself, so its Withings account can be linked
again. Account deletion (``ownvitals.auth.purge``) uses the webhook purge
the same way before the account cascades.
"""

from uuid import UUID

from sqlalchemy import Column, MetaData, Table, delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai.tables import ai_conversations, ai_messages
from ownvitals.connections.repository import delete_disconnected_row
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.tables import findings, metric_daily, observations, scores
from ownvitals.ingest.tables import data_revisions, sync_cursors
from ownvitals.notify.tables import notify_holds
from ownvitals.withings.tables import (
    activity_daily,
    devices,
    ecg_recordings,
    intraday,
    measure_groups,
    measures,
    sleep_series,
    sleep_summaries,
    workouts,
)

Factory = async_sessionmaker[AsyncSession]
# A narrow handle for the queue table, which has no Core table in this package.
_JOBS = Table("jobs", MetaData(), Column("owner_id"))
# Children before parents. Jobs last, so a failure leaves the queue row to retry from.
#
# The derived tables (#181) are health data computed from the imported rows, so they go with
# them (#218). They are per owner, not per connection, and so is this purge: it removes the
# user's imported rows of every connection (a user has at most one live Withings connection,
# ADR 0004 section 7), so it removes all of the user's derived rows too. No foreign key links
# them to the imported tables or to each other, so their place in the order is free; they go
# before the queue, so a failure still leaves the job to retry from.
IMPORTED = (
    measures,
    measure_groups,
    devices,
    ecg_recordings,
    sleep_summaries,
    activity_daily,
    workouts,
    sleep_series,
    intraday,
    sync_cursors,
    data_revisions,
    observations,
    metric_daily,
    findings,
    notify_holds,  # #195: the notification holds of those findings (rule ids and times only)
    scores,
    # #206: the AI conversation history is text about this data, so it goes with it (messages
    # before their conversations). The consent itself is kept (``KEPT``).
    ai_messages,
    ai_conversations,
    _JOBS,
)
# Owned tables this option deliberately keeps, because they hold no imported or derived health
# data: ``user_profiles`` (settings) and ``mail_outbox`` (notification state: a fixed kind, a
# route and its send state, ADR 0007; Architect decision on #225), and the AI cost cap's
# ``ai_spend`` and ``ai_reservations`` (micro-USD accounting, ADR 0014 section 7; #205): spend
# that was billed must stay counted, or deleting imported data would reset the month's budget.
# ``ai_consents`` (#206): the record of the user's AI consent and its withdrawal, no content;
# deleting data is not withdrawing consent (withdrawal is its own action, ``ai.consent``).
# ``ai_audit`` (#207): one redacted row per model call (counts, codes, keyed fingerprints; no
# content), kept like the spend it accounts for.
# All go with the account through the ``owner_id`` cascade. The connection row is removed by
# ``delete_disconnected_row`` and ``job_events`` follow their job, so neither is listed here or
# in ``IMPORTED``. A test enumerates every owned table against these lists, so a new owned table
# must be added to one of them.
KEPT = (
    "user_profiles",
    "mail_outbox",
    "ai_spend",
    "ai_reservations",
    "ai_consents",
    "ai_audit",
)


async def purge_imported(factory: Factory, ctx: UserContext) -> None:
    """Remove this user's imported rows and the analysis rows derived from them. The Withings
    row, the user's settings, their mail outbox and their AI spend ledger (``KEPT``) stay.

    The session is this user's, and every statement names their id, so another
    user's rows are left alone. The queue's events follow their job.
    """
    async with user_session(factory, ctx) as db:
        for table in IMPORTED:
            await db.execute(delete(table).where(table.c.owner_id == ctx.user_id))


async def purge_webhook_events(factory: Factory, ctx: UserContext) -> None:
    """This owner's webhook events, through the definer. It finds them through the owner's
    connection rows, so it runs while those rows still exist."""
    async with user_session(factory, ctx) as db:
        await db.execute(text("SELECT public.webhook_events_purge_owner()"))


async def purge_connection(factory: Factory, ctx: UserContext, connection_id: UUID) -> None:
    """Imported rows, this owner's webhook events, then the disconnected row. A row that is
    still live is left in place."""
    await purge_imported(factory, ctx)
    await purge_webhook_events(factory, ctx)
    await delete_disconnected_row(factory, ctx, connection_id)
