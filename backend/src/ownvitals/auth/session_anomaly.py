"""The session anomaly signal in the session check (#461, ADR 0020).

``deps.resolve_request`` calls ``observe`` once per request whose session cookie resolved to a
valid session. In one ``system_session`` transaction it

1. takes the user's row ``FOR KEY SHARE`` and then the session's row ``FOR NO KEY UPDATE`` (the
   lock order of ``userlock``: an account delete or a rotation in flight finishes first, and a
   gone session is left alone);
2. scores the request against the session's history (``session_anomaly_policy.assess``) and
   stores the session's new counter and country;
3. when a signal fires for the first time in this session, sets ``anomaly_at`` and writes one
   ``session_anomaly`` ``auth_events`` row (``detail``: the signal codes only; the address and
   coarse origin as every event has them), which the user sees in "Recent security activity"
   and the admin abuse view lists.

After the commit, a recorded anomaly gets one WARNING line on ``ownvitals.auth.anomaly``: the
signal codes and the user and session ids. Never the address (not even in part), the
``User-Agent`` string, the place, a token or a cookie.

**It is only a signal**: nothing here signs a session out, refuses a request or changes what the
session may do (ADR 0020 section 3).
"""

import logging
import re
from typing import Final
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import geo
from ownvitals.auth.events import record_event
from ownvitals.auth.session_anomaly_policy import KIND, History, Verdict, assess
from ownvitals.auth.session_anomaly_policy import Request as Seen
from ownvitals.auth.tables import sessions, users
from ownvitals.config import Settings
from ownvitals.db import system_session

logger = logging.getLogger("ownvitals.auth.anomaly")

LOG_MESSAGE: Final = "session anomaly"
_COUNTRY: Final = re.compile(r"[A-Z0-9]{2}")  # ``geo``'s country code, the column's check


def country_of(place: str | None) -> str | None:
    """The country code of a ``geo`` place ("City, CC" or "CC"), or None."""
    if not place:
        return None
    code = place.rsplit(", ", 1)[-1]
    return code if _COUNTRY.fullmatch(code) else None


def _agent(request: Request) -> str | None:
    values = request.headers.getlist("user-agent")
    return values[0] if len(values) == 1 else None


async def observe(
    request: Request, session_id: UUID, user_id: UUID, ip: str | None
) -> Verdict | None:
    """Score this request against its session and record the first anomaly (module docstring).
    None when the session went meanwhile."""
    settings: Settings = request.app.state.settings
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    country = country_of(geo.place(request, settings))
    async with system_session(factory) as db:
        verdict = await _observe(db, session_id, user_id, ip, _agent(request), country)
    if verdict is not None and verdict.record:
        logger.warning(
            LOG_MESSAGE,
            extra={
                "signals": list(verdict.signals),
                "user_id": str(user_id),
                "session_id": str(session_id),
            },
        )
    return verdict


async def _observe(
    db: AsyncSession,
    session_id: UUID,
    user_id: UUID,
    ip: str | None,
    agent: str | None,
    country: str | None,
) -> Verdict | None:
    lock = select(users.c.id).where(users.c.id == user_id)
    await db.execute(lock.with_for_update(read=True, key_share=True))  # FOR KEY SHARE
    row = (
        await db.execute(
            select(
                sessions.c.ip,
                sessions.c.user_agent,
                sessions.c.seen_country,
                sessions.c.seen_window_at,
                sessions.c.seen_count,
                sessions.c.anomaly_at,
                func.now().label("now"),
            )
            .where(sessions.c.id == session_id, sessions.c.user_id == user_id)
            .with_for_update(key_share=True)
        )
    ).first()
    if row is None:
        return None  # signed out, rotated or deleted meanwhile
    history = History(
        ip=row.ip,
        user_agent=row.user_agent,
        country=row.seen_country,
        window_at=row.seen_window_at,
        window_count=row.seen_count,
        flagged=row.anomaly_at is not None,
    )
    verdict = assess(history, Seen(ip=ip, user_agent=agent, country=country, now=row.now))
    values: dict[str, object] = {
        "seen_window_at": verdict.window_at,
        "seen_count": verdict.window_count,
        "seen_country": verdict.country,
    }
    if verdict.record:
        values["anomaly_at"] = row.now
    await db.execute(update(sessions).where(sessions.c.id == session_id).values(**values))
    if verdict.record:
        await record_event(
            db, KIND, user_id=user_id, ip=ip, detail={"signals": list(verdict.signals)}
        )
    return verdict
