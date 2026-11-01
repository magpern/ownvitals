"""The auth side of the operator CLI (ADR 0003 section 1, "Admins"; classbeat ADR 0003 §7).

``python -m ownvitals invite create``, ``admin grant``, ``admin revoke`` and ``admin mfa-reset``
run as the app role through these functions; ``ownvitals.cli`` opens no session itself (only
``ownvitals.db`` and ``ownvitals.auth`` may, ``tests/session_scan.py``). Each runs in one
``system_session``; ``mfa_reset`` mails the reset link after its transaction committed.
"""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import accounts
from ownvitals.auth.hooks import register_reset_link_sender, send_reset_link
from ownvitals.auth.reset_routes import make_reset_link_sender
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.mail import Mailer

Factory = async_sessionmaker[AsyncSession]
AccountError = accounts.AccountError


async def create_invite(factory: Factory, expires_in: timedelta) -> str:
    """One invite code, shown once (only its SHA-256 is stored)."""
    async with system_session(factory) as db:
        return await accounts.create_invite(db, expires_in)


async def grant_admin(factory: Factory, email: str) -> bool:
    async with system_session(factory) as db:
        return await accounts.grant_admin(db, email)


async def revoke_admin(factory: Factory, settings: Settings, email: str) -> bool:
    async with system_session(factory) as db:
        return await accounts.revoke_admin(db, settings, email)


async def mfa_reset(factory: Factory, settings: Settings, email: str) -> bool:
    """Remove the user's TOTP, recovery codes and known devices, revoke every session and write
    ``mfa_reset`` (one transaction), then mail a reset link. Returns whether it was mailed;
    raises ``AccountError`` for an unknown address and ``MailError`` when the mail fails (the
    reset itself stays done)."""
    async with system_session(factory) as db:
        user = await accounts.reset_mfa(db, email)
    register_reset_link_sender(make_reset_link_sender(settings, factory, Mailer(settings)))
    try:
        return await send_reset_link(user.id, user.email)
    finally:
        register_reset_link_sender(None)
