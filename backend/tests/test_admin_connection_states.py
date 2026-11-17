"""Connection states and counts in the admin area (issue 549; ADR 0002 sections 3 and 4).

- ``GET /api/admin/users`` and ``/users/{id}`` carry each account's Withings state
  (``healthy``, ``needs_reconnect``, ``not_connected``), read through the definer
  ``admin_withings_states`` in the same audited transaction.
- ``GET /api/admin/overview`` (``overview.read``): accounts, second factors, accounts per
  Withings state (the definer ``admin_withings_counts``) and three job counts.
- ``GET /api/admin/withings-app`` (``withings_app.read``): whether an app is configured, the
  last four characters of its client id and the API host; never a secret, the callback or the
  whole id.

The definers refuse without their transaction's ``ok`` row, inside a user session and for a bad
argument; maint reads no column of ``connections`` and the app role cannot call them; in the
admin transaction maint still reads no owner or health column (the RLS probe). Every account,
connection and app row is synthetic; the planted markers must never come back.
"""

import itertools
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    caller,
)
from contexttools import admin_probe, assert_admin_isolated, owned_tables, plant
from jobtools import admin_url, engine_for, new_job
from ownvitals.admin.db import Actor, admin_session
from ownvitals.admin.withings_app import client_hint

# Planted in the synthetic rows; none may appear in any response.
SYNTHETIC_USERID = 987654321  # synthetic Withings user ids start here
_USERIDS = itertools.count(SYNTHETIC_USERID)
SYNTHETIC_APP_CLIENT = "synthetic-client-0123456789abcdef"
SYNTHETIC_CALLBACK = "https://synthetic-callback.example.org"
SECRET_MARKER = b"synthetic-secret-marker"
_STATES = text("SELECT * FROM public.admin_withings_states(CAST(:ids AS uuid[]))")
_COUNTS = text("SELECT * FROM public.admin_withings_counts()")
_AUDIT = text(
    "INSERT INTO admin_audit (actor_id, actor_email, action, target_user_id, params, outcome, "
    "request_id, ip_hash) VALUES (:actor, 'synthetic-admin@example.com', :action, :target, "
    "CAST('{}' AS jsonb), :outcome, 'synthetic-request', :ip_hash)"
)
ACCOUNT_FIELDS = {
    "id",
    "email",
    "role",
    "disabled_at",
    "created_at",
    "second_factor",
    "last_sign_in",
    "sessions",
    "withings",
}


async def _connection(
    aenv: AdminEnv, owner: UUID, status: str, *, provider: str = "withings"
) -> None:
    """A synthetic connection row: filler ciphertext for a connected one, none otherwise."""
    userid = next(_USERIDS)  # unique per app, and never shown
    public = f'{{"app_id": 1, "withings_userid": {userid}}}'
    sealed = status == "connected"
    await aenv.superuser(
        "INSERT INTO connections (owner_id, provider, status, public, secret, secret_nonce, "
        "wrapped_dek, dek_nonce, key_id) VALUES (:o, :p, :s, CAST(:pub AS jsonb), :secret, "
        ":nonce, :wrapped, :nonce, :key)",
        o=owner,
        p=provider,
        s=status,
        pub=public if provider == "withings" else "{}",
        secret=SECRET_MARKER if sealed else None,
        nonce=b"\x02" * 12 if sealed else None,
        wrapped=b"\x03" * 48 if sealed else None,
        key="kek-synthetic" if sealed else None,
    )


async def _household(aenv: AdminEnv) -> dict[str, UUID]:
    """Five synthetic accounts, one per case."""
    ids = {
        "healthy": await aenv.env.user("alex@example.org"),
        "broken": await aenv.env.user("sam@example.org"),
        "none": await aenv.env.user("kim@example.org"),
        "gone": await aenv.env.user("robin@example.org"),
        "test": await aenv.env.user("jo@example.org"),
    }
    await _connection(aenv, ids["healthy"], "connected")
    await _connection(aenv, ids["broken"], "needs_reconnect")
    await _connection(aenv, ids["gone"], "disconnected")  # not live
    await _connection(aenv, ids["test"], "connected", provider="test")  # not Withings
    return ids


def _no_secrets(body: str) -> None:
    assert str(SYNTHETIC_USERID)[:6] not in body
    assert SECRET_MARKER.decode() not in body
    assert "kek-synthetic" not in body
    assert SYNTHETIC_APP_CLIENT not in body
    assert SYNTHETIC_CALLBACK not in body


# --- the Users view's Withings column ---------------------------------------------------------


async def test_the_users_list_shows_each_accounts_withings_state(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    ids = await _household(aenv)
    response = await admin.get("/api/admin/users")
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["users"]}
    assert all(set(row) == ACCOUNT_FIELDS for row in rows.values())
    states = {name: rows[str(user_id)]["withings"] for name, user_id in ids.items()}
    assert states == {
        "healthy": "healthy",
        "broken": "needs_reconnect",
        "none": "not_connected",
        "gone": "not_connected",
        "test": "not_connected",
    }
    assert rows[str(admin.user_id)]["withings"] == "not_connected"
    _no_secrets(response.text)
    assert [r.action for r in await aenv.audit_rows()] == ["user.list"]


async def test_one_account_carries_its_state(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    ids = await _household(aenv)
    response = await admin.get(f"/api/admin/users/{ids['broken']}")
    assert response.status_code == 200, response.text
    assert set(response.json()) == ACCOUNT_FIELDS
    assert response.json()["withings"] == "needs_reconnect"
    _no_secrets(response.text)
    (row,) = await aenv.audit_rows()
    assert (row.action, row.outcome, row.target_user_id) == ("user.read", "ok", ids["broken"])


# --- the Overview -----------------------------------------------------------------------------


async def test_the_overview_counts_and_is_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)  # an account with a confirmed authenticator
    await _household(aenv)  # five more, none with one
    with engine_for(admin_url()) as engine, engine.begin() as connection:
        new_job(connection, admin.user_id)
        new_job(connection, admin.user_id)
        new_job(connection, admin.user_id, state="completed", finished_at=datetime.now(UTC))
    response = await admin.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "accounts": {"total": 6, "second_factor_on": 1, "signup": "invite"},
        "connections": {"healthy": 1, "needs_reconnect": 1, "not_connected": 4},
        "jobs": {"queued": 2, "running": 0, "dead_letter": 0},
    }
    _no_secrets(response.text)
    assert str(admin.user_id) not in response.text
    (row,) = await aenv.audit_rows()
    assert (row.action, row.outcome, row.params, row.target_user_id) == (
        "overview.read",
        "ok",
        {},
        None,
    )


async def test_a_non_admin_gets_no_overview_and_no_row(aenv: AdminEnv) -> None:
    user = await caller(aenv, "user@example.org", role="user")
    for path in ("/api/admin/overview", "/api/admin/withings-app"):
        assert (await user.get(path)).status_code == 403
    assert await aenv.audit_rows() == []


# --- the Withings app card ---------------------------------------------------------------------


async def test_the_withings_app_card_shows_only_its_non_secret_parts(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    response = await admin.get("/api/admin/withings-app")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "configured": False,
        "client_hint": None,
        "api_host": "wbsapi.withings.net",
    }
    await aenv.superuser(
        "INSERT INTO withings_apps (client_id, callback_base, client_secret, client_secret_nonce, "
        "client_wrapped_dek, client_dek_nonce, client_key_id, webhook_secrets) VALUES "
        "('synthetic-older-client', :base, :secret, :nonce, :wrapped, :nonce, 'kek-synthetic', "
        "'[]'), (:client, :base, :secret, :nonce, :wrapped, :nonce, 'kek-synthetic', "
        "CAST(:hooks AS jsonb))",
        client=SYNTHETIC_APP_CLIENT,
        base=SYNTHETIC_CALLBACK,
        secret=SECRET_MARKER,
        nonce=b"\x02" * 12,
        wrapped=b"\x03" * 48,
        hooks='[{"key_id": "kek-synthetic", "hmac": "synthetic-hmac"}]',
    )
    try:
        response = await admin.get("/api/admin/withings-app")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "configured": True,
            "client_hint": "…cdef",  # the newest row's
            "api_host": "wbsapi.withings.net",
        }
        _no_secrets(response.text)
        assert "synthetic-hmac" not in response.text
        assert "synthetic-older-client" not in response.text
    finally:
        await aenv.superuser("DELETE FROM withings_apps")
    rows = await aenv.audit_rows()
    assert [(r.action, r.outcome, r.params) for r in rows] == [("withings_app.read", "ok", {})] * 2


def test_the_client_hint_cuts_only_a_long_enough_id() -> None:
    assert client_hint(None) is None
    assert client_hint("short-id") is None
    assert client_hint("x" * 11) is None
    assert client_hint("synthetic-a91f") == "…a91f"


# --- the definers ------------------------------------------------------------------------------


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
    action: str = "user.list",
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


def _refused(aenv: AdminEnv, planted: dict[str, Any], call: Any, args: dict[str, Any]) -> None:
    with _maint(aenv) as connection:
        _audit(connection, **planted)
        with pytest.raises(DBAPIError, match="no_audit_row"):
            connection.execute(call, args)


async def test_the_state_definer_refuses_without_this_transactions_ok_row(aenv: AdminEnv) -> None:
    actor = await aenv.env.user("admin@example.org", role="admin")
    plain = await aenv.env.user("plain@example.org")
    other = await aenv.env.user("other@example.org")
    gone = await aenv.env.user("gone-admin@example.org", role="admin")
    await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=gone)
    await _connection(aenv, plain, "needs_reconnect")
    one = {"ids": [plain]}
    with _maint(aenv) as connection, pytest.raises(DBAPIError, match="no_audit_row"):
        connection.execute(_STATES, one)  # no audit row at all
    with engine_for(aenv.maint_url) as engine, engine.begin() as connection:  # committed earlier
        connection.execute(text("SET TRANSACTION READ WRITE"))
        _audit(connection, actor)
    for planted in (
        {"actor": actor, "action": "overview.read"},  # another action
        {"actor": actor, "outcome": "refused"},  # not an ok row
        {"actor": actor, "target": plain},  # user.list aimed at an account
        {"actor": actor, "action": "user.read", "target": other},  # another account's read
        {"actor": plain},  # the actor is not an admin
        {"actor": gone},  # the actor is a disabled admin
    ):
        _refused(aenv, planted, _STATES, one)
    _refused(
        aenv,
        {"actor": actor, "action": "user.read", "target": plain},
        _STATES,
        {"ids": [plain, other]},
    )  # a read of one account asks about one
    with _maint(aenv) as connection:  # inside a user session
        _audit(connection, actor)
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(plain)})
        with pytest.raises(DBAPIError, match="bad_context"):
            connection.execute(_STATES, one)
    for ids in ([], [UUID(int=n + 1) for n in range(102)], [plain, None]):
        with _maint(aenv) as connection:
            _audit(connection, actor)
            with pytest.raises(DBAPIError, match="bad_argument"):
                connection.execute(_STATES, {"ids": ids})
    with _maint(aenv) as connection:  # the matching rows: the read goes through
        _audit(connection, actor)
        rows = connection.execute(_STATES, {"ids": [plain, other, UUID(int=7)]}).mappings().all()
        assert sorted((r["user_id"], r["state"]) for r in rows) == sorted(
            [(plain, "needs_reconnect"), (other, "not_connected")]
        )  # an id that names no account is left out
        assert set(rows[0]) == {"user_id", "state"}
    with _maint(aenv) as connection:
        _audit(connection, actor, action="user.read", target=plain)
        rows = connection.execute(_STATES, one).all()
        assert [tuple(r) for r in rows] == [(plain, "needs_reconnect")]


async def test_the_count_definer_refuses_without_its_ok_row(aenv: AdminEnv) -> None:
    actor = await aenv.env.user("admin@example.org", role="admin")
    plain = await aenv.env.user("plain@example.org")
    await _connection(aenv, plain, "connected")
    for planted in (
        {"actor": actor},  # user.list is not overview.read
        {"actor": actor, "action": "overview.read", "outcome": "error"},
        {"actor": actor, "action": "overview.read", "target": plain},
        {"actor": plain, "action": "overview.read"},
    ):
        _refused(aenv, planted, _COUNTS, {})
    with _maint(aenv) as connection:
        _audit(connection, actor, action="overview.read")
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(plain)})
        with pytest.raises(DBAPIError, match="bad_context"):
            connection.execute(_COUNTS)
    with _maint(aenv) as connection:
        _audit(connection, actor, action="overview.read")
        rows = connection.execute(_COUNTS).mappings().all()
        assert [dict(r) for r in rows] == [
            {"state": "healthy", "accounts": 1},
            {"state": "needs_reconnect", "accounts": 0},
            {"state": "not_connected", "accounts": 1},
        ]


async def test_maint_reads_no_connection_column_and_the_app_cannot_call_the_definers(
    aenv: AdminEnv, app_database_url: str
) -> None:
    for column in ("status", "owner_id", "provider"):
        with (
            _maint(aenv) as connection,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            connection.execute(text(f"SELECT {column} FROM connections"))  # noqa: S608
    for call, args in ((_STATES, {"ids": [UUID(int=1)]}), (_COUNTS, {})):
        with (
            engine_for(app_database_url) as engine,
            engine.connect() as connection,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            connection.execute(call, args)


async def test_the_admin_transaction_reads_no_owner_or_health_column(
    aenv: AdminEnv, database_url: str
) -> None:
    """RLS in the admin context: maint bypasses row security, so its grants are the wall. The
    probe reads every owned table of two planted users inside a real ``user.list`` transaction,
    after the state definer ran for both."""
    tables = owned_tables(database_url)
    admin_id = await aenv.env.user("admin@example.org", role="admin")
    a = plant(database_url, await aenv.env.user("ctx-states-a@example.org"))
    b = plant(database_url, await aenv.env.user("ctx-states-b@example.org"))
    actor = Actor(admin_id, "admin@example.org", secrets.token_hex(16), bytes(32))
    params = {"offset": 0, "limit": 50}
    db = aenv.env.app.state.admin_db
    async with admin_session(db, actor, "user.list", params=params) as session:
        found = (await session.execute(_STATES, {"ids": [a.user, b.user]})).all()
        report = await admin_probe(session, (a, b), tables)
    assert {r.user_id for r in found} == {a.user, b.user}
    assert_admin_isolated(report, tables, write=False)
