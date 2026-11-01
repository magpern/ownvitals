"""``SecurityNotifier`` (``ownvitals.auth.notices``) implemented as mail (ADR 0003 section 7).

The callers (#39's routes) run the notices as background tasks and log a failure by its class
only, so a mail error here simply propagates.
"""

from uuid import UUID

from ownvitals.auth import mails
from ownvitals.mail import Mailer


class MailNotifier:
    def __init__(self, mailer: Mailer) -> None:
        self._mailer = mailer

    async def totp_enrolled(self, user_id: UUID, email: str) -> None:
        await self._mailer.send(mails.totp_enrolled_notice(email))

    async def mfa_failures(self, user_id: UUID, email: str) -> None:
        await self._mailer.send(mails.mfa_failures_notice(email))
