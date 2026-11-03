"""Web's outbox sender (ADR 0007 section 2; #194): web only, never the worker.

A background task of the web process (``start``, from ``ownvitals.web``'s lifespan). Every
``INTERVAL_SECONDS`` it claims up to ``BATCH`` rows through ``mail_outbox_claim`` and, for each:

1. resolves the recipient through ``ownvitals.auth`` (guard 8) at the time it sends: the
   account's verified email, or the admins' addresses for an owner notice. No address is stored;
2. builds the mail from the kind's fixed template (``ownvitals.notify.templates``), which says
   only that something is waiting and links to ``public_base_url`` plus the route's path (T59,
   T12);
3. sends it through the guard-listed mail module (``ownvitals.mail``), then marks the row through
   ``mail_outbox_mark``: ``sent``; ``retry`` when a delivery failed (at most 5 attempts, then
   failed); ``failed`` when there is no one to send to or the row has no template.

**The worker cannot send.** ``run_once`` calls ``refuse_in_worker`` before it claims anything, so
in the worker it raises ``OutboundBlocked`` and leaves every row pending; ``Mailer.send`` refuses
there too, before any connection; and no worker module imports this one
(``tests/egress_worker.py``).

The rate is bounded: ``BATCH`` rows every ``INTERVAL_SECONDS`` (an owner notice is one mail per
admin). The first round runs one interval after start, not at once. Logs carry counts only, never
an address, a kind's text or a row id.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import recipients
from ownvitals.egress import refuse_in_worker
from ownvitals.mail import Mailer, MailError, MailMessage
from ownvitals.notify.outbox import ClaimedMail, Outcome, claim, mark
from ownvitals.notify.templates import render

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
UserAddress = Callable[[Factory, UUID], Awaitable[str | None]]
AdminAddresses = Callable[[Factory], Awaitable[list[str]]]

BATCH: Final = 5
LEASE_SECONDS: Final = 300  # BATCH rows at the mail module's 20 s deadline each, with room
INTERVAL_SECONDS: Final = 30.0


@dataclass(slots=True)
class Report:
    """What one round did, as counts."""

    sent: int = 0
    retried: int = 0
    failed: int = 0

    def add(self, outcome: Outcome) -> None:
        if outcome == "sent":
            self.sent += 1
        elif outcome == "retry":
            self.retried += 1
        else:
            self.failed += 1


class OutboxSender:
    def __init__(
        self,
        factory: Factory,
        mailer: Mailer,
        public_base_url: str,
        *,
        batch: int = BATCH,
        lease_seconds: int = LEASE_SECONDS,
        user_address: UserAddress = recipients.user_address,
        admin_addresses: AdminAddresses = recipients.admin_addresses,
    ) -> None:
        self._factory = factory
        self._mailer = mailer
        self._base = public_base_url
        self._batch = batch
        self._lease = lease_seconds
        self._user_address = user_address
        self._admin_addresses = admin_addresses

    def __repr__(self) -> str:
        return "<OutboxSender>"

    async def run_once(self) -> Report:
        """One round: claim, send, mark. Raises ``OutboundBlocked`` in the worker before it
        claims anything."""
        refuse_in_worker(None)
        report = Report()
        for row in await claim(self._factory, self._batch, self._lease):
            outcome = await self._deliver(row)
            await mark(self._factory, row.id, outcome)
            report.add(outcome)
        return report

    async def _deliver(self, row: ClaimedMail) -> Outcome:
        try:
            mail = render(row.kind, row.route, self._base)
        except ValueError:
            return "failed"
        if row.owner_id is None:
            addresses = await self._admin_addresses(self._factory)
        else:
            address = await self._user_address(self._factory, row.owner_id)
            addresses = [] if address is None else [address]
        if not addresses:
            return "failed"
        try:
            for address in addresses:
                await self._mailer.send(
                    MailMessage(to=address, subject=mail.subject, body=mail.body)
                )
        except MailError:
            return "retry"
        return "sent"

    async def run(self, interval: float = INTERVAL_SECONDS) -> None:
        """Rounds forever, one interval apart, the first after one interval. A failed round is
        logged by its exception class and the next one runs."""
        while True:
            await asyncio.sleep(interval)
            try:
                report = await self.run_once()
            except Exception as exc:  # a background task must never die on one bad round
                logger.warning("outbox round failed (%s)", type(exc).__name__)
                continue
            if report.sent or report.retried or report.failed:
                logger.info(
                    "outbox round: %d sent, %d to retry, %d failed",
                    report.sent,
                    report.retried,
                    report.failed,
                )


def start(factory: Factory, mailer: Mailer, public_base_url: str) -> asyncio.Task[None] | None:
    """Start the sender as a task of the running loop (web's lifespan); ``None`` when no SMTP
    host is configured, so mail stays in the outbox until one is. Each round refuses in the
    worker before it claims anything (``run_once``)."""
    if not mailer.configured:
        logger.warning("no SMTP host is configured; outbox mail stays queued")
        return None
    sender = OutboxSender(factory, mailer, public_base_url)
    return asyncio.ensure_future(sender.run())


async def stop(task: asyncio.Task[None] | None) -> None:
    """Cancel the sender's task and wait for it to end."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
