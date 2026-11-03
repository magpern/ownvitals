"""RLS in the web-request context (ADR 0002 section 4; issue #15). Synthetic users and rows.

The real app (``create_app``: lifespan, startup role check, the app role's engine) with a
test-only route that signs the caller in through ``ownvitals.auth`` (``require_user``: a full
session, the issuer of web contexts) and runs ``contexttools.probe`` in a ``user_session`` of
that context. The request body names the other user's ids, as a hostile client would; the owner
of every write is the issued context's user, never the body's.
"""

from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    session_cookie,
)
from contexttools import Owner, assert_isolated, owned_tables, plant, probe, snapshot
from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.db import user_session

PATH = "/api/test-only/contexts/probe"


class Body(BaseModel):
    mine: dict[str, str]  # the caller's planted row ids; ``user`` is ignored
    other: dict[str, str]  # the other user's ids, as a hostile request would name them


router = APIRouter()


@router.post(PATH)
async def probe_route(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)], body: Body
) -> dict[str, Any]:
    mine = Owner.from_json({**body.mine, "user": str(ctx.user_id)})
    other = Owner.from_json(body.other)
    async with user_session(request.app.state.session_factory, ctx) as db:
        return await probe(db, mine, other, request.app.state.ctx_tables)


async def call(env: Env, user: UUID, mine: Owner, other: Owner) -> dict[str, Any]:
    issued = await env.full_login(user)
    info = await env.client.get("/api/auth/session", headers=session_cookie(issued.token))
    headers = {
        **session_cookie(issued.token),
        "Origin": ORIGIN,
        "X-CSRF-Token": info.json()["csrf_token"],
    }
    response = await env.client.post(
        PATH, json={"mine": mine.as_json(), "other": other.as_json()}, headers=headers
    )
    env.client.cookies.clear()
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def web(env: Env, database_url: str) -> Env:
    env.app.include_router(router)
    env.app.state.ctx_tables = owned_tables(database_url)
    return env


async def test_a_web_request_reaches_only_its_users_rows(web: Env, database_url: str) -> None:
    a = plant(database_url, await web.user("ctx-web-a@example.com"))
    b = plant(database_url, await web.user("ctx-web-b@example.com"))
    tables = owned_tables(database_url)
    before = snapshot(database_url, (a, b))
    for me, other in ((a, b), (b, a)):
        assert_isolated(await call(web, me.user, me, other), me, tables)
    assert snapshot(database_url, (a, b)) == before
