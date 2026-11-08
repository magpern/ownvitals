"""``/api/connections``: list, connect and disconnect (ADR 0004 section 7).

Connect and disconnect need recent authentication and a step-up (``require_sensitive``).
Connect offers only the test-only provider, registered when ``environment`` is ``test``, so
production has no connect target here. Bodies that fail validation are refused without
echoing them.

**Withings disconnect** (PLAN section 4.3, ADR 0004 section 7; #163). The revoke runs in the
worker: web makes no Withings call but the code exchange (roadmap Amendment 1). The route
enqueues a ``withings_disconnect`` job for the live row, naming the session that passed the
step-up, in a slot of its own (other jobs of the owner never block it), and waits for it up to
``REVOKE_WAIT_SECONDS``. Then it reads the row:

- the job cleared it: ``revoke_failed`` is what the job recorded on the row;
- the row is still live (no job could be queued, the job did not finish in time, failed
  before the revoke, or no worker runs the kind): the job is cancelled if still queued, the
  route clears that row's credentials itself and records ``revoke_failed``. Secrets never
  outlive the request, and neither route ever refuses for a full queue.

``?delete_imported=true`` then removes the imported rows, this owner's webhook events and the
disconnected row (``connections.purge.purge_connection``). The answer is ``DisconnectOut``; with
``revoke_failed`` it carries the instruction to remove access in the Withings account. The
account deletion route (``auth.email_routes``) uses ``revoke_withings`` the same way.

The request and answer bodies live in ``schemas.py``.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text

from ownvitals.auth.deps import current_session, require_sensitive, require_user
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.purge import purge_connection
from ownvitals.connections.repository import (
    ConnectionInfo,
    connect,
    disconnect,
    list_connections,
)
from ownvitals.connections.schemas import ConnectionOut, ConnectionsOut, DisconnectOut, TestConnect
from ownvitals.connections.tables import PROVIDER_NAME, TEST_PROVIDER, live_providers
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.jobs import service
from ownvitals.jobs.user import NewJob
from ownvitals.withings.disconnect import DISCONNECT_KIND, DisconnectInput

router = APIRouter(prefix="/api/connections", tags=["connections"])
logger = logging.getLogger(__name__)

WITHINGS = "withings"
# How long a disconnect or an account deletion waits for the worker's revoke.
REVOKE_WAIT_SECONDS = 25.0
_POLL_SECONDS = 0.25
_WAITING = frozenset({"queued", "running"})
_ACTIVE_JOBS = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE kind = :k) AS disconnects "
    "FROM jobs WHERE owner_id = :o AND state IN ('queued', 'running')"
)


class Session(Protocol):
    """The signed-in session (``auth.deps.current_session``): only its id is used here."""

    @property
    def id(self) -> UUID: ...


_STATUS = {
    "not_connected": 409,
    "decrypt_failed": 409,
    "needs_reconnect": 409,
    "key_unavailable": 503,
    "rate_limited": 429,
    "upstream_unavailable": 503,
    "revoke_failed": 502,
    "unknown": 502,
}


def _unknown() -> HTTPException:
    return HTTPException(status_code=404, detail={"reason": "unknown_provider"})


def _offered(request: Request, provider: str) -> bool:
    return PROVIDER_NAME.fullmatch(provider) is not None and provider in live_providers(
        request.app.state.settings.environment
    )


def _disconnectable(request: Request, provider: str) -> bool:
    return provider == WITHINGS or _offered(request, provider)


def _live(rows: list[ConnectionInfo], provider: str) -> ConnectionInfo | None:
    return next(
        (row for row in rows if row.provider == provider and row.status != "disconnected"), None
    )


def _job(connection_id: UUID, session_id: UUID) -> NewJob:
    payload = DisconnectInput(connection_id=connection_id, session_id=session_id)
    stored = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    canonical = json.dumps(
        {"input": payload.model_dump(mode="json"), "kind": DISCONNECT_KIND},
        sort_keys=True,
        separators=(",", ":"),
    )
    return NewJob(
        kind=DISCONNECT_KIND,
        input=stored,
        idempotency_key=secrets.token_urlsafe(24),
        request_hash=hashlib.sha256(canonical.encode()).digest(),
    )


async def _ended(request: Request, ctx: UserContext, job_id: UUID) -> bool:
    """Wait for the job to leave ``queued`` and ``running``; ``False`` at the deadline."""
    factory = request.app.state.session_factory
    deadline = time.monotonic() + REVOKE_WAIT_SECONDS
    while True:
        view = await service.get(factory, ctx, job_id)
        if view is None or view.state not in _WAITING:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_POLL_SECONDS)


async def _reserved_limit(request: Request, ctx: UserContext) -> int | None:
    """The limit that gives this enqueue its own slot, outside the shared per-user count.

    Webhook, scheduler and backfill jobs count towards ``jobs_per_user`` but must never block
    a disconnect or an account deletion, so the limit is the owner's active jobs plus one.
    At most one ``withings_disconnect`` is queued or running per owner: ``None`` when one
    already is (the caller then clears locally).
    """
    async with user_session(request.app.state.session_factory, ctx) as db:
        row = (await db.execute(_ACTIVE_JOBS, {"o": ctx.user_id, "k": DISCONNECT_KIND})).one()
    return None if row.disconnects else int(row.total) + 1


async def _enqueue(
    request: Request, ctx: UserContext, info: Session, connection_id: UUID
) -> UUID | None:
    """The job id, or ``None`` when no job could be queued (never an error to the caller)."""
    try:
        limit = await _reserved_limit(request, ctx)
        if limit is None:
            return None
        created = await service.create(
            request.app.state.session_factory, ctx, _job(connection_id, info.id), limit=limit
        )
    except Exception as exc:  # the local clear below still runs: secrets never outlive this
        logger.warning("disconnect job not queued", extra={"error_type": type(exc).__name__})
        return None
    if created.job is None:
        logger.info("disconnect job not queued", extra={"reason": created.reason})
        return None
    return created.job.id


async def revoke_withings(
    request: Request, ctx: UserContext, info: Session, connection_id: UUID
) -> bool:
    """Revoke upstream through the worker, then make sure the credentials are gone.

    Returns ``revoke_failed``. Web never calls Withings here: the job does. When no job could
    be queued, or it did not clear the row in time, this clears that row itself and records
    the failure. It never refuses: a full queue cannot block a disconnect or a deletion.
    """
    factory = request.app.state.session_factory
    job_id = await _enqueue(request, ctx, info, connection_id)
    if job_id is not None and not await _ended(request, ctx, job_id):
        await service.cancel(factory, ctx, job_id)
    rows = await list_connections(factory, ctx)
    row = next((item for item in rows if item.id == connection_id), None)
    if row is not None and row.status != "disconnected":
        await disconnect(factory, ctx, WITHINGS, revoke_failed=True, connection_id=connection_id)
        return True
    return row is None or row.last_error == "revoke_failed"


def _failure(failure: ConnectionFailure) -> JSONResponse:
    return JSONResponse(
        {"detail": {"reason": failure.category}},
        status_code=_STATUS.get(failure.category, 502),
    )


@router.get("")
async def get_connections(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ConnectionsOut:
    rows = await list_connections(request.app.state.session_factory, ctx)
    return ConnectionsOut(connections=[ConnectionOut.of(row) for row in rows])


@router.post("/{provider}", response_model=ConnectionOut)
async def connect_provider(
    provider: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
) -> ConnectionOut | JSONResponse:
    if not _offered(request, provider) or provider != TEST_PROVIDER:
        raise _unknown()
    invalid = False
    try:
        raw: Any = await request.json()
    except ValueError:
        invalid = True
        raw = None
    body: TestConnect | None = None
    if not invalid:
        try:
            body = TestConnect.model_validate(raw)
        except ValidationError:
            invalid = True
    if invalid or body is None:
        raise HTTPException(status_code=400, detail={"reason": "invalid_body"})
    try:
        info = await connect(
            request.app.state.session_factory,
            request.app.state.keyring,
            ctx,
            provider,
            body.access_token.get_secret_value(),
            body.refresh_token.get_secret_value(),
        )
    except ConnectionFailure as failure:
        return _failure(failure)
    return ConnectionOut.of(info)


@router.delete("/{provider}")
async def disconnect_provider(
    provider: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
    info: Annotated[Session | None, Depends(current_session)],
    delete_imported: bool = False,
) -> DisconnectOut:
    if not _disconnectable(request, provider):
        raise _unknown()
    factory = request.app.state.session_factory
    if provider != WITHINGS:
        await disconnect(factory, ctx, provider)
        return DisconnectOut.of(revoke_failed=False, imported_deleted=False)
    live = _live(await list_connections(factory, ctx), WITHINGS)
    if live is None or info is None:
        raise HTTPException(status_code=409, detail={"reason": "not_connected"})
    failed = await revoke_withings(request, ctx, info, live.id)
    if delete_imported:
        await purge_connection(factory, ctx, live.id)
    return DisconnectOut.of(revoke_failed=failed, imported_deleted=delete_imported)
