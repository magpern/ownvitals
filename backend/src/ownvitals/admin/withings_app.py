"""The admin Withings app card (issue 549): ``GET /api/admin/withings-app``
(``withings_app.read``).

Read only, in one audited read ``admin_session`` on ``ownvitals_maint``, from the newest
``withings_apps`` row (the app the connect routes use, ``max(id)``) through maint's column grant
on its non-secret columns. The browser gets three things:

- ``configured``: whether a row exists;
- ``client_hint``: the last four characters of its client id after an ellipsis (``…a91f``), as
  the mock-up shows it, and nothing for an id too short to cut;
- ``api_host``: the Withings API host the worker calls (``ownvitals.egress``, code, not a row).

Never the client secret, its envelope, the webhook secrets, the callback URL or the whole
client id. The configuration changes only from the operator command line (``ownvitals
withings-apps``).
"""

from typing import Final

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ownvitals import egress
from ownvitals.admin.db import admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db

router = admin_router()

_NEWEST = text("SELECT client_id FROM withings_apps ORDER BY id DESC LIMIT 1")
HINT_LENGTH: Final = 4
# An id shorter than this is not cut: four characters of it would be most of it.
MIN_CUT_LENGTH: Final = 12


class WithingsAppOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured: bool
    client_hint: str | None
    api_host: str


def client_hint(client_id: str | None) -> str | None:
    """``…`` and the last four characters, or ``None`` for no id or one too short to cut."""
    if client_id is None or len(client_id) < MIN_CUT_LENGTH:
        return None
    return "…" + client_id[-HINT_LENGTH:]


@router.get("/withings-app", response_model=WithingsAppOut)
async def read_withings_app(request: Request, actor: AdminActor) -> WithingsAppOut:
    async with admin_session(require_db(request), actor, "withings_app.read") as db:
        client_id = (await db.execute(_NEWEST)).scalar_one_or_none()
    return WithingsAppOut(
        configured=client_id is not None,
        client_hint=client_hint(client_id),
        api_host=egress.WITHINGS_API_HOST,
    )
