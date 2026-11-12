"""The admin abuse view (#461, ADR 0017): ``GET /api/admin/anomalies``.

Refused without the admin gate (``test_admin_routes_guard`` covers every kind of caller) and,
although a read, without a step-up within the admin window; the refusal is audited. Past both,
the read is one audited ``anomaly.list`` transaction on the maint role that reads through the
definer ``admin_session_anomalies`` (70-admin-accounts.sql), which refuses without that
transaction's ``ok`` row. Maint itself cannot read ``auth_events``, the app role cannot call the
definer, and in that admin transaction maint still reads no owner or health column (the RLS
probe of ``contexttools``). Synthetic accounts and documentation-range addresses only.
"""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, ProgrammingError

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    age_session,
    caller,
    reason,
)
from contexttools import admin_probe, assert_admin_isolated, owned_tables, plant
from jobtools import engine_for
from ownvitals.admin.db import Actor, admin_session

PATH = "/api/admin/anomalies"
PLANTED_IP = "198.51.100.77"
_CALL = text("SELECT * FROM public.admin_session_anomalies(:n, NULL, NULL)")
_AUDIT = text(
    "INSERT INTO admin_audit (actor_id, actor_email, action, target_user_id, params, outcome, "
    "request_id, ip_hash) VALUES (:actor, 'synthetic-admin@example.com', :action, :target, "
    "CAST('{}' AS jsonb), :outcome, 'synthetic-request', :ip_hash)"
)


async def _event(
    aenv: AdminEnv, user_id: UUID | None, *, kind: str = "session_anomaly", age: str = "1 hour"
) -> UUID:
    row = await aenv.env.row(
        "INSERT INTO auth_events (kind, user_id, ip, at, detail, browser, os, place) VALUES "
        "(:k, :u, :ip, now() - CAST(:age AS interval), "
        'CAST(\'{"signals": ["new_ip_and_agent", "planted_code"]}\' AS jsonb), '
        "'Firefox', 'Linux', 'SE') RETURNING id",
        k=kind,
        u=user_id,
        ip=PLANTED_IP,
        age=age,
    )
    return row.id


def _anomaly_rows(rows: list[Any]) -> list[Any]:
    return [r for r in rows if r.action == "anomaly.list"]


async def test_the_read_needs_a_step_up_and_the_refusal_is_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv, stepup=False)
    response = await admin.get(PATH)
    assert (response.status_code, reason(response)) == (403, "step_up_required")
    await age_session(aenv, admin, mfa=None, stepup="5 minutes 5 seconds")
    response = await admin.get(PATH)
    assert (response.status_code, reason(response)) == (403, "step_up_required")
    rows = await aenv.audit_rows()
    assert [(r.action, r.outcome, r.reason) for r in rows] == [
        ("admin.request", "refused", "step_up_required")
    ] * 2
    assert rows[0].params == {"method": "GET", "route": PATH}


async def test_a_non_admin_is_refused_without_a_row(aenv: AdminEnv) -> None:
    user = await caller(aenv, "user@example.com", role="user")
    response = await user.get(PATH)
    assert (response.status_code, reason(response)) == (403, "forbidden")
    assert await aenv.audit_rows() == []


async def test_the_view_lists_recent_anomalies_across_users_and_is_audited(
    aenv: AdminEnv,
) -> None:
    admin = await caller(aenv)
    robin = await aenv.env.user("robin@example.com")
    kim = await aenv.env.user("kim@example.com")
    older = await _event(aenv, robin, age="2 hours")
    newer = await _event(aenv, kim, age="1 hour")
    gone = await _event(aenv, None, age="3 hours")  # the account was deleted
    await _event(aenv, robin, age="31 days")  # past the 30 days
    await _event(aenv, robin, kind="sign_in")  # another kind
    response = await admin.get(PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["id"] for r in body["rows"]] == [str(newer), str(older), str(gone)]
    assert body["next"] is None
    first = body["rows"][0]
    assert set(first) == {"id", "at", "user_id", "email", "signals", "browser", "os", "place"}
    assert (first["user_id"], first["email"]) == (str(kim), "kim@example.com")
    assert first["signals"] == ["new_ip_and_agent"]  # an unknown code is dropped
    assert (first["browser"], first["os"], first["place"]) == ("Firefox", "Linux", "SE")
    assert (body["rows"][2]["user_id"], body["rows"][2]["email"]) == (None, None)
    assert PLANTED_IP not in response.text
    (row,) = _anomaly_rows(await aenv.audit_rows())
    assert (row.outcome, row.actor_id, row.params) == (
        "ok",
        admin.user_id,
        {"limit": 50, "page": "first"},
    )


async def test_the_view_pages_by_its_cursor(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    robin = await aenv.env.user("robin@example.com")
    ids = [await _event(aenv, robin, age=f"{n} hours") for n in (1, 2, 3)]
    seen: list[str] = []
    page = (await admin.get(PATH, params={"limit": 2})).json()
    seen += [r["id"] for r in page["rows"]]
    assert page["next"] is not None
    page = (await admin.get(PATH, params={"limit": 2, **page["next"]})).json()
    seen += [r["id"] for r in page["rows"]]
    assert page["next"] is None
    assert seen == [str(i) for i in ids]
    params = [r.params for r in _anomaly_rows(await aenv.audit_rows())]
    assert params == [{"limit": 2, "page": "first"}, {"limit": 2, "page": "next"}]


async def test_half_a_cursor_is_refused_and_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    response = await admin.get(PATH, params={"before_id": str(UUID(int=1))})
    assert (response.status_code, reason(response)) == (422, "cursor_invalid")
    assert [(r.action, r.outcome, r.reason) for r in await aenv.audit_rows()] == [
        ("admin.request", "refused", "cursor_invalid")
    ]


# --- the definer --------------------------------------------------------------------------------


@contextmanager
def _maint(aenv: AdminEnv) -> Iterator[Connection]:
    """One read-write transaction on the maint role, rolled back at the end."""
    with engine_for(aenv.maint_url) as engine, engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ WRITE"))
        try:
            yield connection
        finally:
            connection.rollback()


def _audit(
    connection: Connection,
    actor: UUID,
    action: str = "anomaly.list",
    outcome: str = "ok",
    target: UUID | None = None,
) -> None:
    connection.execute(
        _AUDIT,
        {
            "actor": actor,
            "action": action,
            "target": target,
            "outcome": outcome,
            "ip_hash": bytes(32),
        },
    )


async def test_the_definer_refuses_without_this_transactions_ok_row(aenv: AdminEnv) -> None:
    actor = await aenv.env.user("admin@example.com", role="admin")
    plain = await aenv.env.user("plain@example.com")
    gone = await aenv.env.user("gone-admin@example.com", role="admin")
    await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=gone)
    await _event(aenv, plain)
    with _maint(aenv) as connection, pytest.raises(DBAPIError, match="no_audit_row"):
        connection.execute(_CALL, {"n": 10})  # no audit row at all
    with engine_for(aenv.maint_url) as engine, engine.begin() as connection:  # committed earlier
        connection.execute(text("SET TRANSACTION READ WRITE"))
        _audit(connection, actor)
    plantings: tuple[dict[str, Any], ...] = (
        {"actor": actor, "action": "user.list"},  # another action
        {"actor": actor, "outcome": "refused"},  # not an ok row
        {"actor": actor, "target": plain},  # aimed at an account
        {"actor": plain},  # the actor is not an admin
        {"actor": gone},  # the actor is a disabled admin
    )
    for planted in plantings:
        with _maint(aenv) as connection:
            _audit(connection, **planted)
            with pytest.raises(DBAPIError, match="no_audit_row"):
                connection.execute(_CALL, {"n": 10})
    with _maint(aenv) as connection:  # inside a user session
        _audit(connection, actor)
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(plain)})
        with pytest.raises(DBAPIError, match="bad_context"):
            connection.execute(_CALL, {"n": 10})
    for limit in (0, 102):
        with _maint(aenv) as connection:
            _audit(connection, actor)
            with pytest.raises(DBAPIError, match="bad_argument"):
                connection.execute(_CALL, {"n": limit})
    with _maint(aenv) as connection:  # the matching row: the read goes through
        _audit(connection, actor)
        rows = connection.execute(_CALL, {"n": 10}).mappings().all()
        assert [r["user_id"] for r in rows] == [plain]
        assert "ip" not in rows[0]


async def test_maint_cannot_read_the_events_and_the_app_cannot_call_the_definer(
    aenv: AdminEnv, app_database_url: str
) -> None:
    with _maint(aenv) as connection, pytest.raises(ProgrammingError, match="permission denied"):
        connection.execute(text("SELECT kind FROM auth_events"))
    with (
        engine_for(app_database_url) as engine,
        engine.connect() as connection,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        connection.execute(_CALL, {"n": 10})


async def test_the_admin_transaction_reads_no_owner_or_health_column(
    aenv: AdminEnv, database_url: str
) -> None:
    """RLS in the admin context: the abuse view's transaction is maint's like any other, and
    maint bypasses row security, so its grants are the wall. The probe reads every owned table
    of two planted users inside a real ``anomaly.list`` transaction, after the definer ran."""
    tables = owned_tables(database_url)
    admin_id = await aenv.env.user("admin@example.com", role="admin")
    a = plant(database_url, await aenv.env.user("ctx-anomaly-a@example.com"))
    b = plant(database_url, await aenv.env.user("ctx-anomaly-b@example.com"))
    await _event(aenv, a.user)
    actor = Actor(admin_id, "admin@example.com", secrets.token_hex(16), bytes(32))
    params = {"limit": 10, "page": "first"}
    db = aenv.env.app.state.admin_db
    async with admin_session(db, actor, "anomaly.list", params=params) as session:
        found = (await session.execute(_CALL, {"n": 10})).all()
        report = await admin_probe(session, (a, b), tables)
    assert [r.user_id for r in found] == [a.user]
    assert_admin_isolated(report, tables, write=False)
