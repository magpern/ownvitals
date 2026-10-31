"""Test-only ways to build a ``UserContext``; the execution-context harness (ADR 0002 s. 4).

``make_context`` builds a context with the issuer token (the runtime barrier refuses any other
way); only tests may do that.

The harness runs the same owned-data operations for one user the way an execution context
reaches owned rows, on the test-only owned table ``t_parent`` (``dbtools``) as the app role:

- ``WebRequest``: the real FastAPI app (``create_app``: its lifespan, the startup role check and
  the app role's engine), a test-only router whose dependency stands in for the ``ownvitals.auth``
  issuer (an opaque session token, never a user id, picks the user), and ``user_session``;
- ``WorkerJob``: a separate engine, as the worker process has, checked at start like the worker
  will be; each operation is a synthetic job row whose context is issued from the row alone, as
  the job issuer of ``ownvitals.jobs`` does (ADR 0005 section 4).

#15 adds the scheduler, webhook and admin contexts on the same interface (``add``, ``names``,
``find``, ``delete``). ``Rows`` is the test-only repository both contexts use: like every real
repository it filters on ``owner_id`` itself, and RLS is the second wall. ``filtered=False``
removes the first wall and ``bypass_url`` (a superuser's engine) the second, so the tests can
show each wall holds alone and that the check fails when both are gone.
"""

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ownvitals.config import Settings
from ownvitals.context import _ISSUER, UserContext  # pyright: ignore[reportPrivateUsage]
from ownvitals.db import (
    create_engine,
    create_session_factory,
    user_session,
    verify_database_role,
)
from ownvitals.web import create_app

Sessions = async_sessionmaker[AsyncSession]


def make_context(
    user_id: UUID | None = None,
    role: str = "user",
    connections: Iterable[tuple[str, UUID]] = (),
) -> UserContext:
    return UserContext(
        user_id=user_id or uuid.uuid4(),
        role=role,  # pyright: ignore[reportArgumentType]
        connections=frozenset(connections),
        issuer=_ISSUER,
    )


INSERT_ROW = text("INSERT INTO t_parent (owner_id, name) VALUES (:owner, :name) RETURNING id")
# filtered -> statement. The unfiltered ones are the deliberately broken double.
NAMES = {
    True: text("SELECT name FROM t_parent WHERE owner_id = :owner ORDER BY name"),
    False: text("SELECT name FROM t_parent ORDER BY name"),
}
FIND = {
    True: text("SELECT name FROM t_parent WHERE id = :id AND owner_id = :owner"),
    False: text("SELECT name FROM t_parent WHERE id = :id"),
}
DELETE = {
    True: text("DELETE FROM t_parent WHERE id = :id AND owner_id = :owner"),
    False: text("DELETE FROM t_parent WHERE id = :id"),
}


@dataclass(frozen=True)
class Rows:
    """The test-only repository of ``t_parent``. ``filtered=False`` drops its owner filter (a
    deliberately broken double); inserts always carry the caller's owner."""

    filtered: bool = True

    async def add(self, db: AsyncSession, ctx: UserContext, name: str) -> UUID:
        return (await db.execute(INSERT_ROW, {"owner": ctx.user_id, "name": name})).scalar_one()

    async def names(self, db: AsyncSession, ctx: UserContext) -> list[str]:
        return list((await db.execute(NAMES[self.filtered], {"owner": ctx.user_id})).scalars())

    async def find(self, db: AsyncSession, ctx: UserContext, row_id: UUID) -> str | None:
        params = {"id": row_id, "owner": ctx.user_id}
        return (await db.execute(FIND[self.filtered], params)).scalar_one_or_none()

    async def delete(self, db: AsyncSession, ctx: UserContext, row_id: UUID) -> bool:
        result = await db.execute(DELETE[self.filtered], {"id": row_id, "owner": ctx.user_id})
        return result.rowcount == 1  # pyright: ignore[reportAttributeAccessIssue]


class ExecutionContext(Protocol):
    """One way of reaching owned rows (ADR 0002 section 4), for one user per call."""

    name: str

    async def add(self, user: UUID, name: str) -> UUID: ...
    async def names(self, user: UUID) -> list[str]: ...
    async def find(self, user: UUID, row_id: UUID) -> str | None: ...
    async def delete(self, user: UUID, row_id: UUID) -> bool: ...


def settings_for(app_url: str) -> Settings:
    return Settings(database_url=app_url, environment="test")  # pyright: ignore[reportArgumentType]


@asynccontextmanager
async def bypass_sessions(superuser_url: str) -> AsyncIterator[Sessions]:
    """A session factory of the superuser, which bypasses RLS even on forced tables. Only for
    proving that a repository's own owner filter holds: ``create_engine`` refuses this role."""
    engine = create_async_engine(superuser_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


# --- web request ---------------------------------------------------------------------------------


class Named(BaseModel):
    name: str


router = APIRouter(prefix="/api/test-only/rows")


async def current_context(
    request: Request, x_test_session: Annotated[str | None, Header()] = None
) -> UserContext:
    """Stands in for the ``ownvitals.auth`` dependency: a session token, never a user id, from
    the request; the user comes from the server-side record of that session."""
    user = request.app.state.test_sessions.get(x_test_session or "")
    if user is None:
        raise HTTPException(status_code=401)
    return make_context(user)


Ctx = Annotated[UserContext, Depends(current_context)]


def _rows(request: Request) -> tuple[Sessions, Rows]:
    return request.app.state.session_factory, request.app.state.test_rows


@router.post("")
async def add_row(request: Request, ctx: Ctx, body: Named) -> dict[str, str]:
    sessions, rows = _rows(request)
    async with user_session(sessions, ctx) as db:
        return {"id": str(await rows.add(db, ctx, body.name))}


@router.get("")
async def list_rows(request: Request, ctx: Ctx) -> dict[str, list[str]]:
    sessions, rows = _rows(request)
    async with user_session(sessions, ctx) as db:
        return {"names": await rows.names(db, ctx)}


@router.get("/{row_id}")
async def get_row(request: Request, ctx: Ctx, row_id: UUID) -> Named:
    sessions, rows = _rows(request)
    async with user_session(sessions, ctx) as db:
        name = await rows.find(db, ctx, row_id)
    if name is None:  # not found and not owned are the same answer
        raise HTTPException(status_code=404)
    return Named(name=name)


@router.delete("/{row_id}", status_code=204)
async def delete_row(request: Request, ctx: Ctx, row_id: UUID) -> Response:
    sessions, rows = _rows(request)
    async with user_session(sessions, ctx) as db:
        deleted = await rows.delete(db, ctx, row_id)
    if not deleted:
        raise HTTPException(status_code=404)
    return Response(status_code=204)


class WebRequest:
    """Each operation is one HTTP request to the running app, signed in as the user."""

    name = "web request"

    def __init__(self, app: FastAPI, client: httpx.AsyncClient) -> None:
        self.app, self.client = app, client
        self._tokens: dict[UUID, str] = {}

    def _headers(self, user: UUID) -> dict[str, str]:
        if user not in self._tokens:  # "sign in": a fresh opaque session token for the user
            token = uuid.uuid4().hex
            self.app.state.test_sessions[token] = user
            self._tokens[user] = token
        return {"x-test-session": self._tokens[user]}

    async def add(self, user: UUID, name: str) -> UUID:
        response = await self.client.post("/api/test-only/rows", json={"name": name},
                                          headers=self._headers(user))  # fmt: skip
        response.raise_for_status()
        return UUID(response.json()["id"])

    async def names(self, user: UUID) -> list[str]:
        response = await self.client.get("/api/test-only/rows", headers=self._headers(user))
        response.raise_for_status()
        return response.json()["names"]

    async def find(self, user: UUID, row_id: UUID) -> str | None:
        response = await self.client.get(
            f"/api/test-only/rows/{row_id}", headers=self._headers(user)
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["name"]

    async def delete(self, user: UUID, row_id: UUID) -> bool:
        response = await self.client.delete(
            f"/api/test-only/rows/{row_id}", headers=self._headers(user)
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True


@asynccontextmanager
async def web_request(
    app_url: str, *, rows: Rows | None = None, bypass_url: str | None = None
) -> AsyncIterator[WebRequest]:
    app = create_app(settings_for(app_url))
    app.include_router(router)
    app.state.test_sessions = {}
    app.state.test_rows = rows or Rows()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        if bypass_url is not None:
            app.state.session_factory = await stack.enter_async_context(bypass_sessions(bypass_url))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        )
        yield WebRequest(app, client)


# --- worker job ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticJob:
    """A claimed job row, reduced to what the issuer reads: its owner, kind and input."""

    owner_id: UUID
    kind: str
    input: dict[str, Any]


def issue_from_job(job: SyntheticJob) -> UserContext:
    """Stands in for the job issuer (ADR 0005 section 4): the owner from the claimed row alone,
    always ``role="user"``."""
    return make_context(job.owner_id, "user")


class WorkerJob:
    """Each operation is one synthetic job, run in a ``user_session`` of its issued context."""

    name = "worker job"

    def __init__(self, sessions: Sessions, rows: Rows) -> None:
        self.sessions, self.rows = sessions, rows

    async def run(self, job: SyntheticJob) -> Any:
        ctx = issue_from_job(job)
        async with user_session(self.sessions, ctx) as db:
            if job.kind == "add":
                return await self.rows.add(db, ctx, job.input["name"])
            if job.kind == "names":
                return await self.rows.names(db, ctx)
            if job.kind == "find":
                return await self.rows.find(db, ctx, job.input["id"])
            if job.kind == "delete":
                return await self.rows.delete(db, ctx, job.input["id"])
        raise ValueError(f"unknown job kind {job.kind!r}")

    async def add(self, user: UUID, name: str) -> UUID:
        return await self.run(SyntheticJob(user, "add", {"name": name}))

    async def names(self, user: UUID) -> list[str]:
        return await self.run(SyntheticJob(user, "names", {}))

    async def find(self, user: UUID, row_id: UUID) -> str | None:
        return await self.run(SyntheticJob(user, "find", {"id": row_id}))

    async def delete(self, user: UUID, row_id: UUID) -> bool:
        return await self.run(SyntheticJob(user, "delete", {"id": row_id}))


@asynccontextmanager
async def worker_job(
    app_url: str, *, rows: Rows | None = None, bypass_url: str | None = None
) -> AsyncIterator[WorkerJob]:
    engine = create_engine(settings_for(app_url))
    try:
        await verify_database_role(engine)  # what the worker's startup will do (#9)
        if bypass_url is None:
            yield WorkerJob(create_session_factory(engine), rows or Rows())
        else:
            async with bypass_sessions(bypass_url) as bypass:
                yield WorkerJob(bypass, rows or Rows())
    finally:
        await engine.dispose()


# Every execution context this harness covers; #15 adds scheduler, webhook and admin.
EXECUTION_CONTEXTS = {"web request": web_request, "worker job": worker_job}
