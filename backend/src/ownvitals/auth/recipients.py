"""Where mail goes and in which language (ADR 0007 section 2, ADR 0015 section 7; #194, #253): the
account tables are ``ownvitals.auth``'s (guard 8), so the senders ask here, at the time they send.
No address and no language is ever stored in the outbox.

- A user's mail goes to the account's verified email (ADR 0003 section 1: every account's email
  is verified), and to no one when the account is gone or disabled.
- An owner notice goes to the owner, that is the admins (ADR 0004 section 6): every admin account
  that is not disabled. It is always English (``notify.locale_policy``).
- The language comes from ``users.locale`` and is only ever returned as a code of the locale
  manifest that is also offered: any other stored value gives ``en``. Its one use is to pick a
  fixed template table. Auth mail and the security notices sent inside a request use the stored
  language, else the request's, else ``en`` (ADR 0015 section 7, amended 2026-11-10).

The addresses are returned to the caller and never logged.
"""

from collections.abc import Collection
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals import locale_policy, locales
from ownvitals.auth.locale import offered
from ownvitals.auth.tables import users
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.notify import locale_policy as mail_locale

Factory = async_sessionmaker[AsyncSession]


async def user_address(factory: Factory, user_id: UUID) -> str | None:
    """The verified email of an active account, or ``None``."""
    async with system_session(factory) as db:
        return (
            await db.execute(
                select(users.c.email).where(
                    users.c.id == user_id,
                    users.c.disabled_at.is_(None),
                    users.c.email_verified_at.is_not(None),
                )
            )
        ).scalar_one_or_none()


async def admin_addresses(factory: Factory) -> list[str]:
    """The verified emails of the active admin accounts, in a stable order."""
    async with system_session(factory) as db:
        rows = await db.execute(
            select(users.c.email)
            .where(
                users.c.role == "admin",
                users.c.disabled_at.is_(None),
                users.c.email_verified_at.is_not(None),
            )
            .order_by(users.c.created_at, users.c.id)
        )
        return [str(email) for email in rows.scalars()]


def usable_locales(enabled: Collection[str]) -> frozenset[str]:
    """The codes mail may use: in the manifest and in ``enabled`` (the offered languages)."""
    return frozenset(code for code in locales.SUPPORTED_LOCALES if code in enabled)


async def _stored_locale(db: AsyncSession, user_id: UUID) -> object:
    return (
        await db.execute(select(users.c.locale).where(users.c.id == user_id))
    ).scalar_one_or_none()


async def user_locale(
    factory: Factory, user_id: UUID, enabled: Collection[str] = locales.SUPPORTED_LOCALES
) -> str:
    """The language of outbox mail to the user: the stored code while it is usable, else
    ``en`` (``NULL``, a code switched off, or anything outside the manifest)."""
    async with system_session(factory) as db:
        stored = await _stored_locale(db, user_id)
    return mail_locale.outbox_locale(usable_locales(enabled), stored)


def request_locale(request: Request, field: object = None) -> str:
    """The request's language (ADR 0015 section 2): the body's ``locale`` field if offered, else
    ``Accept-Language``, else ``en``."""
    codes = offered(request.app.state.settings)
    return locale_policy.request_locale(codes, field, request.headers.get("accept-language"))


async def account_mail_locale(
    db: AsyncSession, settings: Settings, user_id: UUID | None, requested: object
) -> str:
    """The account's stored language if ``user_id`` names one that has a usable code, else
    ``requested`` (the request's language, already resolved) if usable, else ``en``."""
    stored = None if user_id is None else await _stored_locale(db, user_id)
    usable = usable_locales(offered(settings))
    return mail_locale.request_mail_locale(usable, stored, requested)


async def request_mail_locale(
    db: AsyncSession, request: Request, user_id: UUID | None, field: object = None
) -> str:
    """The language of auth mail and security notices sent inside ``request``: the account's
    stored language if ``user_id`` names one that has a usable code, else the request's (always
    so before an account exists, ``user_id`` ``None``)."""
    settings = request.app.state.settings
    return await account_mail_locale(db, settings, user_id, request_locale(request, field))
