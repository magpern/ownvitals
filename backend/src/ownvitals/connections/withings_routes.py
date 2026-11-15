"""The Withings connect over HTTP (PLAN section 4.3, ADR 0004 section 7, roadmap Amendment 1).

``POST /api/connections/withings/start`` needs recent authentication and a step-up
(``require_sensitive``) and the per-IP OAuth start limit. It answers ``{authorize_url}``: the
state is issued by ``withings.oauth.start``, bound to this session, hashed, 10 minutes, one use,
in this web process's ``StateStore`` (``app.state.withings_oauth_states``).

``GET /api/oauth/withings/callback`` is where Withings sends the browser back. It always answers
303 to ``/onboarding?withings=<outcome>`` with ``no-store`` and ``no-referrer``, never an error
body, and never echoes the query (which holds the code and the state; ``ownvitals.log`` strips
``/oauth/`` query strings from every log line). ``withings.oauth.callback`` takes the state
before anything else and then exchanges the code in this request; a denial at Withings consumes
the state too. On success the route queues one ``withings_connect`` job for the stored row (the
worker reads devices, capabilities and subscriptions and queues the backfills), at most one
queued or running per owner. Web makes no other Withings call.

The app is the newest ``withings_apps`` row (``current_app_id``): one app per deployment is what
PLAN section 4.1 describes. With none, start answers 503 ``withings_not_configured``.
"""

import hashlib
import json
import logging
import secrets
from typing import Annotated, Final, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text

from ownvitals.auth.deps import (
    current_context,
    current_session,
    require_sensitive,
    reserve_oauth_start,
)
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.jobs import service
from ownvitals.jobs.user import NewJob
from ownvitals.jobs.withings import ConnectInput
from ownvitals.withings import oauth
from ownvitals.withings.oauth import ConnectRefused, SessionProof, StateStore

router = APIRouter(tags=["connections"])
logger = logging.getLogger(__name__)

START_PATH: Final = "/api/connections/withings/start"
CALLBACK_PATH: Final = "/api/oauth/withings/callback"
RETURN_PATH: Final = "/onboarding"
CONNECT_KIND: Final = "withings_connect"
CONNECTED: Final = "connected"
# Withings' own answers are never echoed: a denial or any other ``error`` is one code.
DENIED: Final = "denied"
FAILED: Final = "error"
_MAX_PARAM = 512
_STATE_ATTR = "withings_oauth_states"
_NEWEST_APP = text("SELECT max(id) FROM withings_apps")
_ACTIVE_JOBS = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE kind = :k) AS connects "
    "FROM jobs WHERE owner_id = :o AND state IN ('queued', 'running')"
)
_START_STATUS = {"step_up_required": 403, "rate_limited": 429}


class SessionInfo(Protocol):
    """The signed-in session (``auth.deps.current_session``): its id and recent step-up."""

    @property
    def id(self) -> UUID: ...

    @property
    def recent_stepup(self) -> bool: ...


class StartOut(BaseModel):
    authorize_url: str


def state_store(request: Request) -> StateStore:
    """This process's OAuth state store, created on first use (one web process, ADR 0004 §7)."""
    store: StateStore | None = getattr(request.app.state, _STATE_ATTR, None)
    if store is None:
        store = StateStore()
        setattr(request.app.state, _STATE_ATTR, store)
    return store


async def current_app_id(request: Request, ctx: UserContext) -> int | None:
    """The newest configured Withings app, or ``None`` when there is none."""
    async with user_session(request.app.state.session_factory, ctx) as db:
        found = (await db.execute(_NEWEST_APP)).scalar_one_or_none()
    return None if found is None else int(found)


def _proof(info: SessionInfo) -> SessionProof:
    return SessionProof(session_id=info.id, stepped_up=info.recent_stepup)


def _no_store(response: JSONResponse | RedirectResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


@router.post(START_PATH, response_model=StartOut)
async def start_withings(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> JSONResponse:
    if info is None:  # require_sensitive already refused this; kept for the type
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"})
    await reserve_oauth_start(request)
    app_id = await current_app_id(request, ctx)
    if app_id is None:
        raise HTTPException(status_code=503, detail={"reason": "withings_not_configured"})
    try:
        started = await oauth.start(
            request.app.state.session_factory,
            request.app.state.keyring,
            ctx,
            _proof(info),
            app_id,
            store=state_store(request),
        )
    except ConnectRefused as refused:
        status = _START_STATUS.get(refused.reason, 409)
        raise HTTPException(status_code=status, detail={"reason": refused.reason}) from None
    except ConnectionFailure as failure:
        logger.warning("withings start refused", extra={"reason": failure.category})
        raise HTTPException(status_code=503, detail={"reason": failure.category}) from None
    response = JSONResponse(StartOut(authorize_url=started.authorize_url).model_dump())
    _no_store(response)
    return response


def _param(request: Request, name: str) -> str | None:
    """One query value, or ``None`` when absent, repeated, empty or too long."""
    values = request.query_params.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > _MAX_PARAM:
        return None
    return values[0]


async def _queue_connect(request: Request, ctx: UserContext, connection_id: object) -> None:
    """One ``withings_connect`` for the stored row, never more than one active per owner. The
    job has a slot of its own: the owner's other jobs never keep the first import from starting.
    A failure is logged; the 6-hourly poll still finds the connection."""
    factory = request.app.state.session_factory
    payload = ConnectInput.model_validate({"connection_id": connection_id})
    stored = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    canonical = json.dumps(
        {"input": payload.model_dump(mode="json"), "kind": CONNECT_KIND},
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        async with user_session(factory, ctx) as db:
            row = (await db.execute(_ACTIVE_JOBS, {"o": ctx.user_id, "k": CONNECT_KIND})).one()
        if row.connects:
            return
        created = await service.create(
            factory,
            ctx,
            NewJob(
                kind=CONNECT_KIND,
                input=stored,
                idempotency_key=secrets.token_urlsafe(24),
                request_hash=hashlib.sha256(canonical.encode()).digest(),
            ),
            limit=int(row.total) + 1,
        )
    except Exception as exc:  # the grant is stored; the poll is the fallback
        logger.warning("withings connect job not queued", extra={"error_type": type(exc).__name__})
        return
    if created.job is None:
        logger.info("withings connect job not queued", extra={"reason": created.reason})


async def _outcome(request: Request, info: SessionInfo | None, ctx: UserContext | None) -> str:
    state = _param(request, "state")
    if ctx is None or info is None or state is None:
        return "state_rejected"
    store = state_store(request)
    if "error" in request.query_params:
        # Denied at Withings (or any other error it reports): the state is spent all the same.
        await store.take(state)
        return DENIED
    code = _param(request, "code")
    if code is None:
        await store.take(state)
        return FAILED
    try:
        stored = await oauth.callback(
            request.app.state.session_factory,
            request.app.state.keyring,
            ctx,
            _proof(info),
            code,
            state,
            store=store,
        )
    except ConnectRefused as refused:
        return refused.reason
    except ConnectionFailure:
        return FAILED
    await _queue_connect(request, ctx, stored.id)
    return CONNECTED


@router.get(CALLBACK_PATH, include_in_schema=False)
async def withings_callback(
    request: Request,
    info: Annotated[SessionInfo | None, Depends(current_session)],
    ctx: Annotated[UserContext | None, Depends(current_context)],
) -> RedirectResponse:
    outcome = await _outcome(request, info, ctx)
    if outcome != CONNECTED:
        logger.info("withings callback refused", extra={"reason": outcome})
    response = RedirectResponse(f"{RETURN_PATH}?withings={outcome}", status_code=303)
    _no_store(response)
    return response
