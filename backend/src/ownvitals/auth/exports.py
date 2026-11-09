"""The export's two entry points into the account tables (ADR 0013 sections 4 and 5; #211).

The account tables are system tables without RLS (ADR 0003 section 1), so only ``ownvitals.auth``
reads them (guard 8). The export builder gets the account's data, and writes its audit events,
only through these two functions, both keyed by ``ctx``'s user alone (the user-scoped rule of
guard 7): no parameter names another user.

- ``export_account(factory, ctx)``: the allowlisted columns of the user's ``users`` row and of
  their own ``auth_events`` (the 30 days that exist), nothing else. ``ACCOUNT_COLUMNS`` and
  ``ACCOUNT_NOT_EXPORTED`` classify every column of both tables, and ``ACCOUNT_TABLES_NOT_EXPORTED``
  every other account table; ``tests/test_reports_export_allowlist.py`` fails when the catalog
  holds a column or table in none of them.
- ``record_event(factory, ctx, kind, detail, ip=...)``: one ``auth_events`` row of the user for
  one of the export kinds, with a typed ``detail`` per kind (a closed set of fields: no free
  text). A detail of the wrong model for its kind is refused before anything is written.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.events import record_event as _record
from ownvitals.auth.tables import auth_events, users
from ownvitals.context import UserContext, require_context
from ownvitals.db import system_session

Factory = async_sessionmaker[AsyncSession]

_OWNER: Final = "the account's own id, given once in manifest.json"
ACCOUNT_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "users": ("id", "email", "email_verified_at", "role", "disabled_at", "created_at", "locale"),
    "auth_events": ("kind", "at", "ip", "browser", "os", "place"),
}
ACCOUNT_NOT_EXPORTED: Final[Mapping[str, Mapping[str, str]]] = {
    "users": {
        "email_normalized": "a normalized copy of the email (the email itself is exported)",
        "password_hash": "the password hash (ADR 0013 section 1)",
        "webauthn_handle": "the passkey user handle (ADR 0013 section 1)",
    },
    "auth_events": {
        "id": "an internal row id",
        "user_id": _OWNER,
        "detail": "internal detail of the event (its kind, time and origin are exported)",
    },
}
ACCOUNT_TABLES_NOT_EXPORTED: Final[Mapping[str, str]] = {
    "identities": "the link to a Google account (an external subject id)",
    "sessions": "session tokens' hashes (ADR 0013 section 1)",
    "known_devices": "device cookies' hashes",
    "pending_signups": "sign-up tokens' hashes",
    "auth_tokens": "mail tokens' hashes",
    "oauth_flows": "OAuth state and PKCE values",
    "invites": "invite codes' hashes",
    "mfa_totp": "the TOTP seed (ciphertext)",
    "mfa_recovery_codes": "recovery codes' hashes",
    "rate_limits": "rate-limit buckets (ADR 0013 section 1)",
    "pre_sessions": "pre-session tokens' hashes",
    "webauthn_credentials": "passkey public keys (ADR 0013 section 1)",
    "webauthn_challenges": "passkey challenges' hashes",
    "qr_logins": "QR sign-in nonces' hashes",
}

ExportEventKind = Literal[
    "export_requested", "export_ready", "export_failed", "export_downloaded", "export_expired"
]
EXPORT_EVENT_KINDS: Final[tuple[str, ...]] = (
    "export_requested",
    "export_ready",
    "export_failed",
    "export_downloaded",
    "export_expired",
)


class _Detail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    export_id: UUID
    export_kind: Literal["data"] = "data"


class Requested(_Detail):
    pass


class Ready(_Detail):
    size: Annotated[int, Field(ge=0, le=1 << 40)]


class Failed(_Detail):
    reason: Literal["export_too_large", "build_failed"]


class Downloaded(_Detail):
    attempt: Annotated[int, Field(ge=1, le=3)]
    completed: bool


class Expired(_Detail):
    pass


DETAILS: Final[Mapping[str, type[_Detail]]] = {
    "export_requested": Requested,
    "export_ready": Ready,
    "export_failed": Failed,
    "export_downloaded": Downloaded,
    "export_expired": Expired,
}


class AccountRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    email_verified_at: datetime
    role: str
    disabled_at: datetime | None
    created_at: datetime
    locale: str | None

    def __repr__(self) -> str:
        return "AccountRow redacted"


class AccountExport(BaseModel):
    """What the export holds of the account tables: exactly ``ACCOUNT_COLUMNS``."""

    model_config = ConfigDict(frozen=True)

    user: AccountRow
    events: tuple[dict[str, Any], ...]

    def __repr__(self) -> str:
        return f"AccountExport({len(self.events)} events)"


async def export_account(factory: Factory, ctx: UserContext) -> AccountExport | None:
    """The user's row and their own security events, allowlisted columns only. ``None`` when the
    account is gone."""
    require_context(ctx)
    user_cols = [users.c[name] for name in ACCOUNT_COLUMNS["users"]]
    event_cols = [auth_events.c[name] for name in ACCOUNT_COLUMNS["auth_events"]]
    async with system_session(factory) as db:
        row = (await db.execute(select(*user_cols).where(users.c.id == ctx.user_id))).first()
        if row is None:
            return None
        events = (
            await db.execute(
                select(*event_cols)
                .where(auth_events.c.user_id == ctx.user_id)
                .order_by(auth_events.c.at, auth_events.c.id)
            )
        ).mappings()
        rows = tuple(dict(event) for event in events)
    return AccountExport(user=AccountRow(**row._asdict()), events=rows)


async def record_event(
    factory: Factory,
    ctx: UserContext,
    kind: ExportEventKind,
    detail: _Detail,
    *,
    ip: str | None = None,
) -> None:
    """One export audit event of ``ctx``'s user, in its own transaction. ``detail`` must be the
    model of ``kind`` exactly (``DETAILS``); anything else raises ``TypeError``, writing nothing."""
    require_context(ctx)
    model = DETAILS.get(kind)
    if model is None or type(detail) is not model:
        raise TypeError("an export event needs its kind's detail model")
    async with system_session(factory) as db:
        await _record(db, kind, user_id=ctx.user_id, ip=ip, detail=detail.model_dump(mode="json"))
