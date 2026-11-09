"""The mail outbox (ADR 0007 sections 1 and 3; #194): what a row holds, how it is written and how
web's sender claims and marks rows.

A row holds a fixed ``kind``, its recipient (an owned row's ``owner_id``, or no one for an owner
notice, which goes to the admins) and a route key from ``ROUTE_PATHS``: never an address, a
subject, free text, a value or finding text. The database checks every kind and route (migrations
``0012_outbox`` and ``0024_outbox_export_ready``), and these lists are the same
(``tests/test_notify_outbox.py``).

- **Mail to a user** (``queue_user_mail``): an owned ``mail_outbox`` row, written in its owner's
  session (inside a job, a fenced write, ADR 0005 section 4), so the policy's ``WITH CHECK``
  stops a job from queuing mail for another user. A second pending row of the same kind for the
  same owner is coalesced into the first.
- **An owner notice** (``queue_owner_notice``): through the definer function
  ``owner_notice_queue``, which takes only the kind; its route comes from a fixed mapping inside
  the function (``OWNER_NOTICE_ROUTES`` mirrors it). At most one pending row per kind.
- **The sender** claims and marks rows only through ``mail_outbox_claim`` and
  ``mail_outbox_mark`` (``claim``, ``mark``), in a session with no user: it holds no
  ``UserContext`` and gets none.

The worker loads this module (``connections.providers.withings`` queues the owner notice of PLAN
section 4.3), so it imports nothing web-only: no ``ownvitals.mail`` (``tests/egress_worker.py``).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.db import system_session

Factory = async_sessionmaker[AsyncSession]

UserKind = Literal["findings_waiting", "connection_attention", "export_ready"]
OwnerNoticeKind = Literal["connection_needs_reconnect"]
UserRoute = Literal["overview", "findings", "connections", "settings_data"]
Outcome = Literal["sent", "failed", "retry"]

USER_KINDS: Final[tuple[str, ...]] = get_args(UserKind)
OWNER_NOTICE_KINDS: Final[tuple[str, ...]] = get_args(OwnerNoticeKind)
USER_ROUTES: Final[tuple[str, ...]] = get_args(UserRoute)
# The fixed mapping inside ``owner_notice_queue``: one route per owner-notice kind.
OWNER_NOTICE_ROUTES: Final[Mapping[str, str]] = MappingProxyType(
    {"connection_needs_reconnect": "admin"}
)
# Route key -> the app's path (``frontend/src/router/appRoutes.ts``). The link in a mail is
# ``public_base_url`` plus this path (T12): the row never holds a URL.
ROUTE_PATHS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "overview": "/",
        "findings": "/findings",
        "connections": "/connections",
        "settings_data": "/settings/data",
        "admin": "/admin",
    }
)
OUTCOMES: Final[tuple[str, ...]] = get_args(Outcome)
# The two tables (migration 0012): the owned one and the system one.
OUTBOX_TABLES: Final = ("mail_outbox", "owner_notices")
# The claim's bounds, as the function checks them.
MAX_BATCH: Final = 100
MIN_LEASE_SECONDS: Final = 30
MAX_LEASE_SECONDS: Final = 600

_QUEUE_USER = text(
    "INSERT INTO public.mail_outbox (owner_id, kind, route) VALUES (:owner, :kind, :route) "
    "ON CONFLICT (owner_id, kind) WHERE state IN ('pending', 'claimed') DO NOTHING "
    "RETURNING id"
)
_QUEUE_OWNER = text("SELECT public.owner_notice_queue(:kind)")
_CLAIM = text("SELECT id, kind, route, owner_id FROM public.mail_outbox_claim(:limit, :lease)")
_MARK = text("SELECT public.mail_outbox_mark(:id, :outcome)")


@dataclass(frozen=True, slots=True)
class ClaimedMail:
    """One claimed row: what the claim returns and nothing else. ``owner_id`` is ``None`` for an
    owner notice (it goes to the admins)."""

    id: UUID
    kind: str
    route: str
    owner_id: UUID | None

    @property
    def owner_notice(self) -> bool:
        return self.owner_id is None


async def queue_user_mail(
    db: AsyncSession, owner_id: UUID, kind: UserKind, route: UserRoute
) -> bool:
    """Queue mail of ``kind`` for ``owner_id``, in that owner's session (``db`` is a
    ``user_session`` or a fenced session of theirs; row-level security refuses any other owner).
    False when a pending row of that kind was already waiting (coalesced)."""
    if kind not in USER_KINDS or route not in USER_ROUTES:
        raise ValueError("not a user-facing outbox kind or route")
    row = (await db.execute(_QUEUE_USER, {"owner": owner_id, "kind": kind, "route": route})).first()
    return row is not None


async def queue_owner_notice(db: AsyncSession, kind: OwnerNoticeKind) -> bool:
    """Queue an owner notice of ``kind`` for the admins, in the caller's transaction. The route
    is the kind's. False when one of that kind was already waiting (coalesced)."""
    if kind not in OWNER_NOTICE_KINDS:
        raise ValueError("not an owner-notice kind")
    return bool((await db.execute(_QUEUE_OWNER, {"kind": kind})).scalar_one())


async def claim(factory: Factory, limit: int, lease_seconds: int) -> list[ClaimedMail]:
    """Claim up to ``limit`` rows for ``lease_seconds``, in a transaction of its own with no
    user. A row not marked within the lease is claimed again (at most 5 attempts in all)."""
    async with system_session(factory) as db:
        rows = (await db.execute(_CLAIM, {"limit": limit, "lease": lease_seconds})).all()
    return [ClaimedMail(id=r.id, kind=r.kind, route=r.route, owner_id=r.owner_id) for r in rows]


async def mark(factory: Factory, mail_id: UUID, outcome: Outcome) -> bool:
    """Mark a claimed row ``sent``, ``failed`` or ``retry``; False when it was not claimed."""
    if outcome not in OUTCOMES:
        raise ValueError("not an outbox outcome")
    async with system_session(factory) as db:
        return bool((await db.execute(_MARK, {"id": mail_id, "outcome": outcome})).scalar_one())
