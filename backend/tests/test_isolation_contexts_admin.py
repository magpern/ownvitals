"""RLS in the admin context (ADR 0002 sections 4 and 5; issue #15). Synthetic users and rows.

The admin area runs as ``ownvitals_maint``, which has ``BYPASSRLS``: what keeps it away from
users' rows is its column allowlist (no owner, no health table) and its read-only default. The
real app with its admin area on maint, an admin signed in through ``ownvitals.auth`` and passed by
``admin_gate``, and two test-only admin routes that run ``contexttools.admin_probe`` in an audited
``admin_session``: a read (``job.list``) and a write (``setting.clear``, after step-up) aimed at
both users' rows. Neither reads an owner or a health column, and neither writes an owned row.
"""

from typing import Any

from fastapi import Request
from pydantic import BaseModel

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
)
from contexttools import (
    Owner,
    admin_probe,
    assert_admin_isolated,
    owned_tables,
    plant,
    snapshot,
)
from ownvitals.admin.db import admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db

PATH = "/test-only/contexts/probe"


class Body(BaseModel):
    owners: list[dict[str, str]]


router = admin_router()


def _owners(body: Body) -> list[Owner]:
    return [Owner.from_json(owner) for owner in body.owners]


@router.post(PATH)
async def read_probe(request: Request, actor: AdminActor, body: Body) -> dict[str, Any]:
    async with admin_session(require_db(request), actor, "job.list", params={"days": 7}) as db:
        return await admin_probe(db, _owners(body), request.app.state.ctx_tables)


@router.put(PATH)
async def write_probe(request: Request, actor: AdminActor, body: Body) -> dict[str, Any]:
    async with admin_session(require_db(request), actor, "setting.clear", write=True) as db:
        return await admin_probe(db, _owners(body), request.app.state.ctx_tables)


async def test_an_admin_route_reads_no_owner_or_health_column_and_writes_no_owned_row(
    aenv: AdminEnv, database_url: str
) -> None:
    app = aenv.env.app
    app.include_router(router)
    tables = owned_tables(database_url)
    app.state.ctx_tables = tables
    admin = await caller(aenv)
    a = plant(database_url, await aenv.env.user("ctx-admin-a@example.com"))
    b = plant(database_url, await aenv.env.user("ctx-admin-b@example.com"))
    body = {"owners": [a.as_json(), b.as_json()]}
    before = snapshot(database_url, (a, b))
    read = await admin.request("POST", "/api/admin" + PATH, json=body)
    assert read.status_code == 200, read.text
    assert_admin_isolated(read.json(), tables, write=False)
    written = await admin.request("PUT", "/api/admin" + PATH, json=body)
    assert written.status_code == 200, written.text
    assert_admin_isolated(written.json(), tables, write=True)
    assert snapshot(database_url, (a, b)) == before
    audited = [(row.action, row.outcome) for row in await aenv.audit_rows()]
    assert ("job.list", "ok") in audited
    assert ("setting.clear", "ok") in audited
