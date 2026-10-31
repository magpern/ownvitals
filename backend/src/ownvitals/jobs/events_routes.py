"""``GET /api/jobs/{id}/events``: the job's progress as server-sent events (ADR 0005 section 7).

Full session only (401; ``ownvitals.jobs.deps``, fail closed until #10). A job that is not the
caller's, an unknown one and a malformed id are the same 404. ``Last-Event-ID`` (or ``?after=``
for the first connect) names the last ``seq`` the browser has. A job that has ended and has no
event after that id answers 204: an ``EventSource`` stops reconnecting on it, and nothing is left
to send. At most ``MAX_STREAMS_PER_USER`` open streams per user and web process (429 beyond). The
stream itself is ``ownvitals.jobs.stream``; every 15 s it asks ``stream_session_probe`` whether
the session is still valid for the same user.

Behind SWAG and Cloudflare (PLAN-operations section 9.2) the response says ``X-Accel-Buffering:
no`` and ``Cache-Control: no-store``; SWAG's location must also not buffer this path (the owner's
SWAG configuration, M2).
"""

import time
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ownvitals.context import UserContext
from ownvitals.jobs import stream
from ownvitals.jobs.deps import Probe, current_user, stream_session_probe
from ownvitals.jobs.progress import read_page

router = APIRouter()

MAX_STREAMS_PER_USER: Final = 5
SEQ_MAX: Final = 2**63 - 1
RETRY_AFTER_SECONDS: Final = 5


class StreamLimiter:
    """Open streams per user in this process. A slot expires a little after the longest stream,
    so one that was never released (a connection that died before its first frame) cannot lock
    a user out for good."""

    def __init__(self) -> None:
        self._slots: dict[UUID, dict[object, float]] = {}

    def acquire(self, user_id: UUID) -> object | None:
        now = time.monotonic()
        slots = {k: d for k, d in self._slots.get(user_id, {}).items() if d > now}
        self._slots[user_id] = slots
        if len(slots) >= MAX_STREAMS_PER_USER:
            return None
        token = object()
        slots[token] = now + stream.MAX_STREAM_SECONDS + 2 * stream.KEEPALIVE_SECONDS
        return token

    def release(self, user_id: UUID, token: object) -> None:
        slots = self._slots.get(user_id)
        if slots is not None:
            slots.pop(token, None)
            if not slots:
                del self._slots[user_id]


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"reason": "not_found"})


def _parse_job_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise _not_found() from None


def _parse_after(request: Request, after: str | None) -> int:
    raw = request.headers.get("last-event-id")
    if raw is None or raw == "":
        raw = after
    if raw is None or raw == "":
        return 0
    if not raw.isascii() or not raw.isdigit() or int(raw) > SEQ_MAX:
        raise HTTPException(status_code=400, detail={"reason": "bad_last_event_id"})
    return int(raw)


@router.get("/{job_id}/events", response_model=None, responses={401: {}, 404: {}, 429: {}})
async def job_events(
    job_id: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(current_user)],
    probe_for: Annotated[Callable[[UUID], Probe], Depends(stream_session_probe)],
    after: str | None = None,
) -> StreamingResponse | JSONResponse | Response:
    uid = _parse_job_id(job_id)
    start = _parse_after(request, after)
    app = request.app
    factory = app.state.session_factory
    first = await read_page(factory, ctx, uid, start, 1)
    if first is None:
        raise _not_found()  # before the limit, so a foreign id never meets a 429
    if first.state in stream.TERMINAL_STATES and not first.events:
        return Response(status_code=204)  # over, and the browser has every event
    limiter: StreamLimiter = app.state.stream_limiter
    slot = limiter.acquire(ctx.user_id)
    if slot is None:
        return JSONResponse(
            {"detail": {"reason": "too_many_streams"}},
            status_code=429,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )

    async def frames() -> AsyncIterator[str]:
        try:
            async for chunk in stream.event_stream(
                factory,
                app.state.event_hub,
                ctx,
                uid,
                start,
                still_signed_in=probe_for(ctx.user_id),
            ):
                yield chunk
        finally:
            limiter.release(ctx.user_id, slot)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
