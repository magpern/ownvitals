"""``PUT /api/account/locale``: the signed-in user's own language (ADR 0015 section 2; #249).

The body is ``{"locale": "sv"}`` or ``{"locale": null}`` ("follow this browser"). A code the
operator does not offer, or a value that is not a string, is refused with ``400
locale_not_offered`` and never echoed. Only the caller's own row is written (the ``UserContext``
of ``require_user``: a full session), under the middleware's Origin and CSRF checks like every
other account write. It is not a step-up action. The language reaches the page in
``GET /api/auth/session``.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ownvitals.auth.deps import require_user
from ownvitals.auth.flowtools import factory
from ownvitals.auth.locale import set_user_locale, still_offered
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/account", tags=["account"])


class LocaleIn(BaseModel):
    # Any JSON value is read and checked against the offered codes below (a wrong type is the
    # same refusal as an unknown code); the key itself is required.
    locale: Any


class LocaleOut(BaseModel):
    locale: str | None


@router.put("/locale")
async def put_locale(
    body: LocaleIn, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> LocaleOut:
    value = body.locale
    chosen = None if value is None else still_offered(request.app.state.settings, value)
    if value is not None and chosen is None:
        raise HTTPException(status_code=400, detail={"reason": "locale_not_offered"})
    async with system_session(factory(request)) as db:
        found = await set_user_locale(db, ctx.user_id, chosen)
    if not found:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    return LocaleOut(locale=chosen)
