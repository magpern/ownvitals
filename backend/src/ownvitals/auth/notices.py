"""Security notices by mail, behind an interface (ADR 0003 section 7).

ownvitals.auth does not send mail itself. The mail sender of #37 implements ``SecurityNotifier``
and registers it once at start-up with ``register_security_notifier``; tests register a fake.
Sending runs after the response (a background task), and a failed or missing sender is logged
without the recipient, so the response never depends on mail.
"""

import logging
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


class SecurityNotifier(Protocol):
    async def totp_enrolled(self, user_id: UUID, email: str) -> None:
        """TOTP was enrolled or replaced on the account."""

    async def mfa_failures(self, user_id: UUID, email: str) -> None:
        """Five failed MFA attempts within an hour."""


_notifier: SecurityNotifier | None = None


def register_security_notifier(notifier: SecurityNotifier | None) -> None:
    global _notifier
    _notifier = notifier


async def notify_totp_enrolled(user_id: UUID, email: str) -> None:
    if _notifier is None:
        logger.warning("no security notifier is registered; the enrollment notice was not sent")
        return
    try:
        await _notifier.totp_enrolled(user_id, email)
    except Exception as exc:  # the class only: the text may name the recipient
        logger.error("sending the TOTP enrollment notice failed: %s", type(exc).__name__)


async def notify_mfa_failures(user_id: UUID, email: str) -> None:
    if _notifier is None:
        logger.warning("no security notifier is registered; the MFA failure notice was not sent")
        return
    try:
        await _notifier.mfa_failures(user_id, email)
    except Exception as exc:
        logger.error("sending the MFA failure notice failed: %s", type(exc).__name__)
