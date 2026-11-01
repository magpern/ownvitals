"""The admin area's audit log (classbeat ADR 0011 section 6; ownvitals #12).

``GET /api/admin/audit``: the ``admin_audit`` rows, newest first, filtered by

- actor (``actor_id``, or ``actor_email`` for an admin whose account has gone and whose id the
  foreign key nulled), action (an exact code, or a prefix ending in ``.`` such as ``setting.``),
  target (``target_user_id``, ``target_kind``, ``target_id``), outcome, and period (``since``
  inclusive, ``until`` exclusive);
- paged by a cursor: ``before`` is the id of the last row of the previous page and the answer's
  ``next_before`` the id to pass for the next one (``null`` on the last page). The ids come from
  one sequence, so a newer row always has a larger id, and a cursor (not an offset) stays right
  while the log grows.

**Read only, and nothing else.** The route is a GET, so the gate never asks for step-up and the
``admin_session`` is a read (``SET TRANSACTION READ ONLY`` right after its own audit row). There
is no route that writes the log: rows come only from ``admin_session`` and ``audit_refusal``.
``ownvitals_maint`` holds ``INSERT`` and column ``SELECT`` on the table and no ``UPDATE``,
``DELETE`` or ``TRUNCATE``; the app role holds nothing (``docs/dev/admin.md``).

**What a row shows**: every column maint may read, never ``ip_hash`` (maint cannot read it), and
``params`` as stored, which ``ownvitals.admin.audit`` validated against its action's schema when
the row was written. The read is audited once (``audit.list``) with its filter in ``params``.
"""

import string
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import text

from ownvitals.admin.audit import (
    OUTCOMES,
    FilterEmail,
    FilterTargetId,
    UnknownAction,
    checked_params,
)
from ownvitals.admin.db import AdminRefused, admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db
from ownvitals.emailnorm import normalize_email

router = admin_router()

ACTION_FILTER = r"^[a-z][a-z0-9_.]{0,59}$"
KIND_FILTER = r"^[a-z][a-z0-9_]{0,39}$"
OUTCOME_FILTER = "^(" + "|".join(OUTCOMES) + ")$"
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

_UPPER = string.ascii_uppercase
_LOWER = string.ascii_lowercase
_COLUMNS = (
    "id, created_at, actor_id, actor_email, action, target_user_id, target_kind, target_id, "
    "params, outcome, reason, request_id"
)
# Every filter is a bound parameter; a NULL parameter turns its condition off.
_LIST = text(
    f"SELECT {_COLUMNS} FROM admin_audit WHERE "  # noqa: S608
    "(CAST(:actor_id AS uuid) IS NULL OR actor_id = CAST(:actor_id AS uuid)) "
    # ASCII letters only on both sides, so neither the database's locale nor Unicode case mapping
    # changes what matches.
    f"AND (CAST(:actor_email AS text) IS NULL OR translate(actor_email, '{_UPPER}', '{_LOWER}') "
    "= CAST(:actor_email AS text)) "
    "AND (CAST(:action AS text) IS NULL OR action = CAST(:action AS text)) "
    "AND (CAST(:action_prefix AS text) IS NULL "
    "OR left(action, length(CAST(:action_prefix AS text))) = CAST(:action_prefix AS text)) "
    "AND (CAST(:target_user_id AS uuid) IS NULL "
    "OR target_user_id = CAST(:target_user_id AS uuid)) "
    "AND (CAST(:target_kind AS text) IS NULL OR target_kind = CAST(:target_kind AS text)) "
    "AND (CAST(:target_id AS text) IS NULL OR target_id = CAST(:target_id AS text)) "
    "AND (CAST(:outcome AS text) IS NULL OR outcome = CAST(:outcome AS text)) "
    "AND (CAST(:since AS timestamptz) IS NULL OR created_at >= CAST(:since AS timestamptz)) "
    "AND (CAST(:until AS timestamptz) IS NULL OR created_at < CAST(:until AS timestamptz)) "
    "AND (CAST(:before AS bigint) IS NULL OR id < CAST(:before AS bigint)) "
    "ORDER BY id DESC LIMIT :n"
)
_CURSOR = text("SELECT 1 FROM admin_audit WHERE id = :id")


class AuditRowOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    actor_id: UUID | None  # None once the admin's account is deleted (actor_email stays)
    actor_email: str
    action: str
    target_user_id: UUID | None
    target_kind: str | None
    target_id: str | None
    params: dict[str, Any]
    outcome: str
    reason: str | None
    request_id: str


class AuditPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: list[AuditRowOut]
    next_before: int | None


@router.get("/audit", response_model=AuditPage)
async def list_audit(
    request: Request,
    actor: AdminActor,
    actor_id: UUID | None = None,
    actor_email: Annotated[FilterEmail | None, Query()] = None,
    action: Annotated[str | None, Query(pattern=ACTION_FILTER)] = None,
    target_user_id: UUID | None = None,
    target_kind: Annotated[str | None, Query(pattern=KIND_FILTER)] = None,
    target_id: Annotated[FilterTargetId | None, Query()] = None,
    outcome: Annotated[str | None, Query(pattern=OUTCOME_FILTER)] = None,
    since: Annotated[AwareDatetime | None, Query()] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
    before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AuditPage:
    if since is not None and until is not None and since >= until:
        raise AdminRefused("period_invalid", 422)  # audited by the AdminRefused handler
    email = normalize_email(actor_email) if actor_email and actor_email.strip() else None
    prefix = action if action is not None and action.endswith(".") else None
    filters: dict[str, Any] = {
        "actor_id": actor_id,
        "actor_email": email,
        "action": None if prefix is not None else action,
        "action_prefix": prefix,
        "target_user_id": target_user_id,
        "target_kind": target_kind,
        "target_id": target_id,
        "outcome": outcome,
        "since": since,
        "until": until,
        "before": before,
    }
    params: dict[str, Any] = {
        **{k: v for k, v in filters.items() if v is not None and k != "action_prefix"},
        "action": action,
        "limit": limit,
    }
    try:  # the normalised filter must still fit its audit row, or 422 (defence in depth)
        checked_params("audit.list", params)
    except UnknownAction:
        raise AdminRefused("filter_invalid", 422) from None
    db = require_db(request)
    async with admin_session(db, actor, "audit.list", params=params) as session:
        if before is not None and (await session.execute(_CURSOR, {"id": before})).first() is None:
            raise AdminRefused("not_found", 404)
        rows = (await session.execute(_LIST, {**filters, "n": limit + 1})).all()
    page = [
        AuditRowOut(
            id=r.id,
            created_at=r.created_at,
            actor_id=r.actor_id,
            actor_email=r.actor_email,
            action=r.action,
            target_user_id=r.target_user_id,
            target_kind=r.target_kind,
            target_id=r.target_id,
            params=dict(r.params),
            outcome=r.outcome,
            reason=r.reason,
            request_id=r.request_id,
        )
        for r in rows[:limit]
    ]
    return AuditPage(rows=page, next_before=page[-1].id if len(rows) > limit else None)
