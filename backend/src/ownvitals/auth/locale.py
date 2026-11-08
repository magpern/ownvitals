"""The account's language (ADR 0015 section 2; #249).

``users.locale`` is ``NULL`` (follow the browser) or a code the operator offers. The statements
live here because guard 8 keeps the account tables in this package. The decisions are
``ownvitals.locale_policy``; this module binds them to a request's settings and header.
"""

from uuid import UUID

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals import locale_policy, locales
from ownvitals.auth.tables import users
from ownvitals.config import Settings


def offered(settings: Settings) -> tuple[str, ...]:
    return locale_policy.offered_locales(locales.SUPPORTED_LOCALES, settings.enabled_locales)


def still_offered(settings: Settings, value: object) -> str | None:
    """A stored or requested code that the operator still offers, else ``None``."""
    return locale_policy.offered_value(offered(settings), value)


def for_new_account(request: Request, field: object) -> str | None:
    """What a new account (its pending row, its OAuth flow) stores: the request's language (the
    body's ``locale`` field, else ``Accept-Language``, else ``en``), or ``None`` while only one
    language is offered."""
    codes = offered(request.app.state.settings)
    chosen = locale_policy.request_locale(codes, field, request.headers.get("accept-language"))
    return locale_policy.storable_locale(codes, chosen)


async def user_locale(db: AsyncSession, settings: Settings, user_id: UUID) -> str | None:
    """The user's own stored language while it is still offered, else ``None``."""
    stored = (
        await db.execute(select(users.c.locale).where(users.c.id == user_id))
    ).scalar_one_or_none()
    return still_offered(settings, stored)


async def set_user_locale(db: AsyncSession, user_id: UUID, locale: str | None) -> bool:
    """Set the user's own language (the caller checked it is offered); ``False`` when the account
    is gone or disabled. The ``UPDATE`` is the transaction's only statement, so it takes the
    user row (``FOR NO KEY UPDATE``) first, as the lock order requires (``userlock``)."""
    result = await db.execute(
        update(users)
        .where(users.c.id == user_id, users.c.disabled_at.is_(None))
        .values(locale=locale)
        .returning(users.c.id)
    )
    return result.first() is not None
