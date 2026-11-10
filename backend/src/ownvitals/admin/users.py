"""The admin area's Users view (#127), on the router shell of #12.

Every route has ``admin_gate``. A write also depends on ``require_admin_stepup`` (the same
five-minute window the gate already applies; the gate runs first, so a missing step-up is
still the audited ``step_up_required``). Account rows are read and changed through
``ownvitals.auth`` (guard 8). A change runs inside its ``admin_session`` (#314): the ``ok``
audit row is that transaction's first statement, then the account definer of
``70-admin-accounts.sql``, which refuses without that row (ADR 0002 section 3). The change and
the row commit together or not at all. A refusal is that session raising ``AdminRefused``:
the ``ok`` row rolls back with it and one ``refused`` row is written.

- ``GET /users?q=&offset=&limit=`` (``user.list``): email, role, whether the authenticator is
  on, live sessions and the latest ``sign_in``. The prefix is not stored, only its length.
- ``GET /users/{user_id}`` (``user.read``): the same for one account. 404 ``not_found``.
- ``POST /users/{user_id}/disable`` and ``.../enable``. Both refuse an admin (409
  ``target_is_admin``); disable revokes every session and keeps an earlier ``disabled_at``.
- ``DELETE /users/{user_id}`` with ``{"email": ...}`` (``user.delete``): the account-delete
  path. The audit row names the account in ``target_user_id``, which the foreign key's
  ``ON DELETE SET NULL`` clears as the account goes in the same transaction, and keeps the id in
  ``target_id``.

No route changes ``users.role`` or resets a second factor. Withings status is not here: reading
it would mean issuing a ``UserContext`` for someone else, and only ``auth.deps`` may do that.
"""

import unicodedata
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.admin.audit import Target
from ownvitals.admin.db import AdminRefused, admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db
from ownvitals.auth.admin_accounts import (
    AccountMissing,
    AccountSummary,
    EmailMismatch,
    TargetIsAdmin,
    account_emails,
    delete_account,
    disable_account,
    enable_account,
    find_account,
    list_accounts,
)
from ownvitals.auth.deps import request_ip, require_admin_stepup
from ownvitals.config import Settings
from ownvitals.context import UserContext

router = admin_router()


def _same_admin(ctx: UserContext, actor: AdminActor) -> None:
    """The step-up dependency and the gate resolved the same admin."""
    if ctx.user_id != actor.user_id:
        raise AdminRefused("forbidden", 403)


class Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class AccountOut(Out):
    id: UUID
    email: str
    role: Literal["user", "admin"]
    disabled_at: datetime | None
    created_at: datetime
    second_factor: Literal["on", "setup_pending"]
    last_sign_in: datetime | None
    sessions: int


class UserPage(Out):
    users: list[AccountOut]
    next_offset: int | None


class DisabledOut(Out):
    id: UUID
    disabled_at: datetime | None
    sessions_revoked: int


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    email: Annotated[str, StringConstraints(min_length=1, max_length=320)]


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _prefix(q: str | None) -> str | None:
    """The address prefix, or ``None``. Control characters are refused (422)."""
    if q is None:
        return None
    prefix = q.strip()
    if not prefix:
        return None
    if any(unicodedata.category(char) == "Cc" for char in prefix):
        raise AdminRefused("invalid_input", 422)
    return prefix


def _shown(row: AccountSummary, email: str) -> AccountOut:
    return AccountOut(
        id=row.id,
        email=email,
        role=row.role,
        disabled_at=row.disabled_at,
        created_at=row.created_at,
        second_factor=row.second_factor,
        last_sign_in=row.last_sign_in,
        sessions=row.sessions,
    )


async def _page(request: Request, rows: list[AccountSummary]) -> list[AccountOut]:
    emails = await account_emails(_factory(request), [row.id for row in rows])
    return [_shown(row, emails[row.id]) for row in rows if row.id in emails]


async def _change[T](
    request: Request,
    actor: AdminActor,
    action: str,
    target: Target,
    change: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """``change`` in the admin transaction whose first statement is the ``ok`` row (#314)."""
    async with admin_session(require_db(request), actor, action, write=True, target=target) as db:
        try:
            return await change(db)
        except AccountMissing:
            raise AdminRefused("not_found", 404) from None
        except TargetIsAdmin:
            raise AdminRefused("target_is_admin", 409) from None
        except EmailMismatch:
            raise AdminRefused("email_mismatch", 409) from None


@router.get("/users", response_model=UserPage)
async def read_users(
    request: Request,
    actor: AdminActor,
    q: Annotated[str | None, Query(max_length=320)] = None,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UserPage:
    prefix = _prefix(q)
    params = {
        "q_length": None if prefix is None else len(prefix),
        "offset": offset,
        "limit": limit,
    }
    async with admin_session(require_db(request), actor, "user.list", params=params):
        found = await list_accounts(
            _factory(request),
            _settings(request),
            prefix=prefix,
            offset=offset,
            limit=limit + 1,
        )
        page = await _page(request, found[:limit])
    return UserPage(users=page, next_offset=offset + limit if len(found) > limit else None)


@router.get("/users/{user_id}", response_model=AccountOut)
async def read_user(request: Request, actor: AdminActor, user_id: UUID) -> AccountOut:
    async with admin_session(
        require_db(request), actor, "user.read", target=Target(user_id=user_id)
    ):
        found = await find_account(_factory(request), _settings(request), user_id)
        if found is None:
            raise AdminRefused("not_found", 404)
        emails = await account_emails(_factory(request), [found.id])
        email = emails.get(found.id)
        if email is None:  # gone in between: inside the session, so no ``ok`` row commits
            raise AdminRefused("not_found", 404)
    return _shown(found, email)


@router.post("/users/{user_id}/disable", response_model=DisabledOut)
async def disable_user(
    request: Request,
    actor: AdminActor,
    user_id: UUID,
    ctx: Annotated[UserContext, Depends(require_admin_stepup)],
) -> DisabledOut:
    _same_admin(ctx, actor)

    async def change(db: AsyncSession) -> tuple[datetime, int]:
        return await disable_account(db, user_id)

    target = Target(user_id=user_id)
    disabled_at, revoked = await _change(request, actor, "user.disable", target, change)
    return DisabledOut(id=user_id, disabled_at=disabled_at, sessions_revoked=revoked)


@router.post("/users/{user_id}/enable", response_model=DisabledOut)
async def enable_user(
    request: Request,
    actor: AdminActor,
    user_id: UUID,
    ctx: Annotated[UserContext, Depends(require_admin_stepup)],
) -> DisabledOut:
    _same_admin(ctx, actor)

    async def change(db: AsyncSession) -> None:
        await enable_account(db, user_id)

    await _change(request, actor, "user.enable", Target(user_id=user_id), change)
    return DisabledOut(id=user_id, disabled_at=None, sessions_revoked=0)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    request: Request,
    actor: AdminActor,
    user_id: UUID,
    body: DeleteIn,
    ctx: Annotated[UserContext, Depends(require_admin_stepup)],
) -> Response:
    _same_admin(ctx, actor)

    async def change(db: AsyncSession) -> None:
        await delete_account(db, user_id, email=body.email, ip=request_ip(request))

    # target_user_id is cleared by the foreign key as the account goes; target_id keeps the id.
    target = Target(user_id=user_id, kind="user", id=str(user_id))
    await _change(request, actor, "user.delete", target, change)
    return Response(status_code=204)
