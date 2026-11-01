"""Hooks between the CLI and the mail flows.

``python -m ownvitals admin mfa-reset`` does the database part and then calls
``send_reset_link``. The CLI registers the real sender (``make_reset_link_sender``), which creates
the ``reset_password`` token and mails the link.
"""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

# (user_id, email) -> None. The CLI registers it.
ResetLinkSender = Callable[[UUID, str], Awaitable[None]]

_reset_link_sender: ResetLinkSender | None = None


def register_reset_link_sender(sender: ResetLinkSender | None) -> None:
    global _reset_link_sender
    _reset_link_sender = sender


async def send_reset_link(user_id: UUID, email: str) -> bool:
    """Mail the user a password-reset link. Returns False when no sender is registered yet."""
    if _reset_link_sender is None:
        logger.warning("no password-reset mail sender is registered; no link was sent")
        return False
    await _reset_link_sender(user_id, email)
    return True
