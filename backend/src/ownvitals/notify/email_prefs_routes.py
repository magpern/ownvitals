"""``/api/notify/email``: the signed-in user's email preference per mail kind (ADR 0017; #459).

- ``GET``: ``kinds``, each suppressible kind (``ownvitals.notify.email_policy``) with
  ``enabled``. Account and security mail and ``export_ready`` are not listed: they cannot be
  switched off.
- ``PUT`` with ``kind`` and ``enabled``: switches that one kind; the answer is the new list. A
  kind that is not suppressible is a 422 (the body model's own check).

A full session for both (``require_user``); the write goes through the auth middleware's Origin
and CSRF checks. Responses carry codes and booleans only.
"""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.notify import email_prefs_store
from ownvitals.notify.email_policy import SuppressibleKind

router = APIRouter(prefix="/api/notify")


class KindOut(BaseModel):
    kind: SuppressibleKind
    enabled: bool


class EmailPrefsOut(BaseModel):
    kinds: list[KindOut]


class EmailPrefIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: SuppressibleKind
    enabled: bool


def _out(prefs: Mapping[str, bool]) -> EmailPrefsOut:
    return EmailPrefsOut(
        kinds=[KindOut.model_validate({"kind": k, "enabled": on}) for k, on in prefs.items()]
    )


@router.get("/email", response_model=EmailPrefsOut)
async def read_email_prefs(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> EmailPrefsOut:
    return _out(await email_prefs_store.read(request.app.state.session_factory, ctx))


@router.put("/email", response_model=EmailPrefsOut)
async def set_email_pref(
    body: EmailPrefIn, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> EmailPrefsOut:
    factory = request.app.state.session_factory
    return _out(await email_prefs_store.set_enabled(factory, ctx, body.kind, body.enabled))
