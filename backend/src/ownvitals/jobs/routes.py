"""``/api/jobs``: create a job, read it, cancel it, follow its events (ADR 0005 sections 2, 7).

- ``POST /api/jobs`` (full session; CSRF from #10) with an ``Idempotency-Key`` header (16 to 64 of
  ``A-Za-z0-9_-``, made by the browser per user action) and ``{kind, input}``. The input is
  validated by the kind's model (``extra="forbid"``). 201 with the new job; the same key and body
  again is 200 with the same job; the same key with another body is 409
  ``idempotency_mismatch``; over the per-user limit of queued and running jobs (the setting
  ``jobs_per_user``, default 2) 429 ``busy`` with ``limit``; 400 for the header; 422 for the
  body, an unknown kind or an input over 16 KiB (``too_large``); 413 ``too_large`` for a body over
  ``BODY_MAX_BYTES``.
- ``GET /api/jobs/{id}``: the job's state, for clients without SSE.
- ``POST /api/jobs/{id}/cancel``: 200 with the state (idempotent); 409 ``not_cancellable`` for a
  job that ended otherwise.
- ``GET /api/jobs/{id}/events``: the event stream (``ownvitals.jobs.events_routes``).

Another user's job id is 404 ``not_found``, the same body as an unknown id or a malformed one.
Errors carry a reason code only: the create body is validated here, and its 422 never echoes a
submitted value, nor the name of a key the model does not know.

Until #10 every route answers 401 (``ownvitals.jobs.deps.current_user``, fail closed), and #9
registers no kind, so the API creates nothing in production yet.

The router's lifespan (merged into the app's by ``include_router``, inside it, so the engine
exists) runs the web side's event hub and the stream limiter: ``app.state.event_hub`` and
``app.state.stream_limiter``. It does not check the queue: the web refusal of a drifted queue is
#77 item 3 (``ownvitals.db.queue_check``).
"""

import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ownvitals.context import UserContext
from ownvitals.jobs import service
from ownvitals.jobs.deps import current_user
from ownvitals.jobs.events_routes import StreamLimiter
from ownvitals.jobs.events_routes import router as events_router
from ownvitals.jobs.hub import EventHub
from ownvitals.jobs.kinds import KIND_NAME, KINDS, KindRegistry
from ownvitals.jobs.sentences import failure_sentence
from ownvitals.jobs.user import NewJob


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    hub = EventHub(app.state.engine)
    app.state.event_hub = hub
    app.state.stream_limiter = StreamLimiter()
    hub.start()
    try:
        yield
    finally:
        await hub.stop()


router = APIRouter(prefix="/api/jobs", tags=["jobs"], lifespan=lifespan)
router.include_router(events_router)

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
INPUT_MAX_BYTES = 16384  # jobs.input's check (migration 0003), measured as jsonb prints it
# A request body of the jobs API is read only up to this.
BODY_MAX_BYTES = 65536

_STATUS = {"not_found": 404, "busy": 429, "too_large": 422}


def job_kinds() -> KindRegistry:
    """The kinds the API accepts: the worker's registry (tests override this dependency)."""
    return KINDS


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=KIND_NAME.pattern)
    input: dict[str, Any]


class JobOut(BaseModel):
    id: UUID
    kind: str
    state: str
    cancel_requested: bool
    error_category: str | None
    error_message: str | None = None  # one plain sentence per category, never error text
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, view: service.JobView) -> "JobOut":
        out = cls.model_validate(view, from_attributes=True)
        out.error_message = failure_sentence(view.state, view.error_category)
        return out


def _refuse(reason: str, status: int | None = None, **counts: int) -> JSONResponse:
    return JSONResponse(
        {"detail": {**counts, "reason": reason}}, status_code=status or _STATUS.get(reason, 409)
    )


def _loc(error: Any) -> list[Any]:
    """Where an error is; for an extra key, its parent only (the key is the client's text)."""
    loc = list(error["loc"])
    return loc[:-1] if error["type"] == "extra_forbidden" else loc


def _invalid(error: ValidationError | None = None) -> JSONResponse:
    """422 without the submitted values (``input``, ``ctx`` and ``url`` are left out)."""
    errors = (
        []
        if error is None
        else [
            {"loc": _loc(e), "type": e["type"]}
            for e in error.errors(include_url=False, include_context=False, include_input=False)
        ]
    )
    return JSONResponse({"detail": {"reason": "invalid_input", "errors": errors}}, status_code=422)


class _TooLarge(Exception):
    pass


async def _body(request: Request) -> bytes:
    """The request body, at most ``BODY_MAX_BYTES`` (read in chunks, never more than that)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > BODY_MAX_BYTES:
        raise _TooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > BODY_MAX_BYTES:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8: the stored input and the request hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def jsonb_size(value: Any) -> int:
    """The length of ``value`` as jsonb prints it (``", "`` and ``": "``), in UTF-8 bytes: what
    the ``jobs_input`` check measures (the database check stays the backstop)."""
    return len(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8"))


@router.post(
    "",
    status_code=201,
    response_model=JobOut,
    responses={200: {"model": JobOut}, 400: {}, 401: {}, 409: {}, 413: {}, 422: {}, 429: {}},
)
async def create_job(
    request: Request,
    ctx: Annotated[UserContext, Depends(current_user)],
    kinds: Annotated[KindRegistry, Depends(job_kinds)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JobOut | JSONResponse:
    if idempotency_key is None:
        return _refuse("idempotency_key_missing", 400)
    if not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        return _refuse("idempotency_key_invalid", 400)
    try:
        body = CreateIn.model_validate_json(await _body(request))
    except _TooLarge:
        return _refuse("too_large", 413)
    except ValidationError as error:
        return _invalid(error)
    kind = kinds.get(body.kind)
    if kind is None:
        return _refuse("unknown_kind", 422)
    try:
        parsed = kind.parse_input(body.input)
    except ValidationError as error:
        return _invalid(error)
    stored = parsed.model_dump(mode="json")
    if jsonb_size(stored) > INPUT_MAX_BYTES:
        return _refuse("too_large")
    new = NewJob(
        kind=kind.name,
        input=canonical_json(stored).decode("utf-8"),
        idempotency_key=idempotency_key,
        request_hash=hashlib.sha256(canonical_json({"kind": kind.name, "input": stored})).digest(),
    )
    result = await service.create(
        request.app.state.session_factory,
        ctx,
        new,
        limit=request.app.state.settings.jobs_per_user,
    )
    if result.job is None:
        reason = result.reason or "invalid_input"
        if result.limit is not None:
            return _refuse(reason, limit=result.limit)
        return _refuse(reason)
    out = JobOut.of(result.job)
    if not result.created:
        return JSONResponse(out.model_dump(mode="json"), status_code=200)
    return out


def _job_id(raw: str) -> UUID | None:
    """The path's job id; ``None`` for a malformed one (answered like an unknown id)."""
    try:
        return UUID(raw)
    except ValueError:
        return None


@router.get("/{job_id}", response_model=JobOut, responses={401: {}, 404: {}})
async def get_job(
    job_id: str, request: Request, ctx: Annotated[UserContext, Depends(current_user)]
) -> JobOut | JSONResponse:
    wanted = _job_id(job_id)
    if wanted is None:
        return _refuse("not_found")
    view = await service.get(request.app.state.session_factory, ctx, wanted)
    if view is None:
        return _refuse("not_found")
    return JobOut.of(view)


@router.post("/{job_id}/cancel", response_model=JobOut, responses={401: {}, 404: {}, 409: {}})
async def cancel_job(
    job_id: str, request: Request, ctx: Annotated[UserContext, Depends(current_user)]
) -> JobOut | JSONResponse:
    wanted = _job_id(job_id)
    if wanted is None:
        return _refuse("not_found")
    view = await service.cancel(request.app.state.session_factory, ctx, wanted)
    if view is None:
        return _refuse("not_found")
    if view.state in ("completed", "failed"):
        return _refuse("not_cancellable")
    return JobOut.of(view)
