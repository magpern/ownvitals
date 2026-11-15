"""RLS in the admin context (ADR 0002 sections 4 and 5; issue #15). Synthetic users and rows.

The admin area runs as ``ownvitals_maint``, which has ``BYPASSRLS``: what keeps it away from
users' rows is its column allowlist (no owner, no health table) and its read-only default. The
real app with its admin area on maint, an admin signed in through ``ownvitals.auth`` and passed by
``admin_gate``, and two test-only admin routes that run ``contexttools.admin_probe`` in an audited
``admin_session``: a read (``job.list``) and a write (``setting.clear``, after step-up) aimed at
both users' rows. Neither reads an owner or a health column, and neither writes an owned row.
``test_the_admin_probe_catches_every_grant`` shows each extra grant to maint fails the probe.
"""

from typing import Any

import pytest
from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import text

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    maint_scripts,
)
from contexttools import (
    Owner,
    admin_probe,
    assert_admin_isolated,
    owned_tables,
    plant,
    snapshot,
    superuser,
)
from jobtools import admin_url, run_script
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


# The grant matrix: maint bypasses row security, so a grant is the only wall. Every privilege
# kind on representative owned tables (the metadata tables maint partly reads, a health table, a
# partitioned one), table-wide and on the owner column alone. Each must fail the probe.
GRANT_TABLES = ("connections", "jobs", "devices", "sleep_series")
GRANTS = (
    "SELECT (owner_id)",
    "SELECT",
    "INSERT (owner_id)",
    "INSERT",
    "UPDATE (owner_id)",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
)
GRANT_MATRIX = [
    pytest.param(table, grant, id=f"{table}-{grant.lower().replace(' ', '')}")
    for table in GRANT_TABLES
    for grant in GRANTS
]


def restore_maint() -> None:
    """``30-maint.sql`` revokes everything maint holds and grants its allowlist again (it keeps
    the password); ``70-admin-accounts.sql`` grants maint its functions again (#314)."""
    run_script(admin_url(), maint_scripts())


@pytest.mark.parametrize(("table", "grant"), GRANT_MATRIX)
async def test_the_admin_probe_catches_every_grant(
    aenv: AdminEnv, database_url: str, table: str, grant: str
) -> None:
    """One extra privilege for maint on one owned table: the admin probe fails on that table."""
    app = aenv.env.app
    app.include_router(router)
    tables = owned_tables(database_url)
    app.state.ctx_tables = tables
    admin = await caller(aenv)
    a = plant(database_url, await aenv.env.user("ctx-admin-ma@example.com"))
    b = plant(database_url, await aenv.env.user("ctx-admin-mb@example.com"))
    body = {"owners": [a.as_json(), b.as_json()]}
    before = snapshot(database_url, (a, b))
    with superuser(database_url) as conn:
        conn.execute(text(f"GRANT {grant} ON public.{table} TO ownvitals_maint"))
    try:
        written = await admin.request("PUT", "/api/admin" + PATH, json=body)
    finally:
        restore_maint()
    assert written.status_code == 200, written.text
    with pytest.raises(AssertionError, match=table):
        assert_admin_isolated(written.json(), tables, write=True)
    assert snapshot(database_url, (a, b)) == before
