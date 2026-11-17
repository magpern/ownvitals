"""The admin page's writes (issue 550): "Invite someone" and "Sign out all".

Each is refused without a step-up from the last 5 minutes and then changes nothing; each commits
with its ``ok`` audit row, which says what and who and never the invite code or its hash. The
definers of ``70-admin-accounts.sql`` refuse without this transaction's matching row. Synthetic
accounts only.
"""

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr
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
from jobtools import engine_for
from ownvitals.admin import account_writes
from ownvitals.admin.route_guard import admin_area_routes
from ownvitals.admin.routes import calls
from ownvitals.auth.admin_writes import NewInvite
from ownvitals.auth.deps import require_admin_stepup
from ownvitals.config import Settings
from ownvitals.web import create_app

_GONE = UUID("00000000-0000-4000-8000-000000000001")
_INVITES = "/api/admin/invites"
_AUDIT = text(
    "INSERT INTO admin_audit (actor_id, actor_email, action, target_user_id, params, outcome, "
    "request_id, ip_hash) VALUES (:actor, 'synthetic-admin@example.com', :action, :target, "
    "CAST('{}' AS jsonb), :outcome, 'synthetic-request', :ip_hash)"
)
_SIGNOUT = text("SELECT outcome FROM public.admin_account_signout(:t)")
_INVITE = text("SELECT public.admin_invite_create(:h, CAST(:e AS interval))")


def _sessions(user_id: UUID) -> str:
    return f"/api/admin/users/{user_id}/sessions"


async def _count(aenv: AdminEnv, sql: str, **params: object) -> int:
    value = await aenv.env.scalar(sql, **params)
    assert isinstance(value, int)
    return value


async def _target(aenv: AdminEnv, email: str = "sam@example.com", sessions: int = 2) -> UUID:
    user_id = await aenv.env.user(email)
    for _ in range(sessions):
        await aenv.env.full_login(user_id)
    return user_id


def test_both_writes_depend_on_require_admin_stepup() -> None:
    app = create_app(Settings(environment="test", app_secret_key=SecretStr("k" * 40)))  # pyright: ignore[reportCallIssue]
    writes = {("POST", "/api/admin/invites"), ("DELETE", "/api/admin/users/{user_id}/sessions")}
    seen: set[tuple[str, str]] = set()
    for path, route in admin_area_routes(app.routes):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            if (method, path) in writes:
                assert require_admin_stepup in calls(route.dependant), (method, path)
                seen.add((method, path))
    assert seen == writes


def test_a_new_invites_repr_leaves_the_code_out() -> None:
    invite = NewInvite(code="synthetic-code-marker", expires_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert "synthetic-code-marker" not in repr(invite)


# --- "Invite someone" --------------------------------------------------------------------------


@pytest.mark.parametrize("stale", [None, "6 minutes"])
async def test_an_invite_without_a_fresh_step_up_is_refused_and_creates_nothing(
    aenv: AdminEnv, stale: str | None
) -> None:
    admin = await caller(aenv, stepup=stale is not None)
    if stale is not None:
        await age_session(aenv, admin, mfa=None, stepup=stale)
    refused = await admin.request("POST", _INVITES)
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    assert "code" not in refused.text
    assert await _count(aenv, "SELECT count(*) FROM invites") == 0
    rows = [(r.action, r.outcome, r.reason, r.params) for r in await aenv.audit_rows()]
    assert rows == [
        (
            "admin.request",
            "refused",
            "step_up_required",
            {"method": "POST", "route": "/api/admin/invites"},
        )
    ]


async def test_an_invite_stores_only_the_hash_and_shows_the_code_once(
    aenv: AdminEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    admin = await caller(aenv)
    before = datetime.now(UTC)
    created = await admin.request("POST", _INVITES)
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    assert set(body) == {"code", "expires_at"}
    code: str = body["code"]
    assert len(code) >= 20
    expires = datetime.fromisoformat(body["expires_at"])
    assert before + timedelta(days=7) - timedelta(minutes=1) < expires
    assert expires < datetime.now(UTC) + timedelta(days=7, minutes=1)

    row = (await aenv.superuser("SELECT * FROM invites")).one()
    assert bytes(row.code_hash) == hashlib.sha256(code.encode()).digest()  # sign-up's token_hash
    assert (row.created_by, row.used_by, row.used_at) == (None, None, None)  # as the CLI stores it
    assert row.expires_at == expires

    audit = await aenv.audit_rows()
    assert [(r.action, r.outcome, r.target_kind, r.target_id, r.target_user_id) for r in audit] == [
        ("invite.create", "ok", "invite", None, None)
    ]
    assert audit[0].params == {"expires_days": 7}
    assert audit[0].actor_id == admin.user_id
    printed = repr([tuple(r) for r in audit])
    assert code not in printed
    assert hashlib.sha256(code.encode()).hexdigest() not in printed
    assert code not in caplog.text

    again = await admin.request("POST", _INVITES)
    assert again.status_code == 201, again.text
    assert again.json()["code"] != code  # a new code each time, never the earlier one again
    assert await _count(aenv, "SELECT count(*) FROM invites") == 2


async def test_a_failed_invite_rolls_back_with_its_ok_row(
    aenv: AdminEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await caller(aenv)
    real = account_writes.create_admin_invite

    async def crashing(*args: Any, **kwargs: Any) -> NewInvite:
        await real(*args, **kwargs)
        raise RuntimeError("synthetic failure after the insert")

    monkeypatch.setattr(account_writes, "create_admin_invite", crashing)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await admin.request("POST", _INVITES)
    assert await _count(aenv, "SELECT count(*) FROM invites") == 0
    rows = [(r.action, r.outcome, r.reason) for r in await aenv.audit_rows()]
    assert rows == [("invite.create", "error", "internal")]


# --- "Sign out all" ----------------------------------------------------------------------------


@pytest.mark.parametrize("stale", [None, "6 minutes"])
async def test_sign_out_all_without_a_fresh_step_up_is_refused_and_signs_no_one_out(
    aenv: AdminEnv, stale: str | None
) -> None:
    admin = await caller(aenv, stepup=stale is not None)
    if stale is not None:
        await age_session(aenv, admin, mfa=None, stepup=stale)
    user_id = await _target(aenv)
    refused = await admin.request("DELETE", _sessions(user_id))
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    assert await _count(aenv, "SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 2
    rows = [(r.action, r.outcome, r.reason) for r in await aenv.audit_rows()]
    assert rows == [("admin.request", "refused", "step_up_required")]


async def test_sign_out_all_deletes_every_session_of_that_account_only(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    user_id = await _target(aenv)
    other = await _target(aenv, "kim@example.com", sessions=1)
    done = await admin.request("DELETE", _sessions(user_id))
    assert done.status_code == 200, done.text
    assert done.json() == {"id": str(user_id), "sessions_revoked": 2}
    assert await _count(aenv, "SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 0
    assert await _count(aenv, "SELECT count(*) FROM sessions WHERE user_id = :u", u=other) == 1
    assert await aenv.env.scalar("SELECT disabled_at IS NULL FROM users WHERE id = :u", u=user_id)
    rows = [(r.action, r.outcome, r.target_user_id, r.params) for r in await aenv.audit_rows()]
    assert rows == [("sessions.revoke_all", "ok", user_id, {})]

    none_left = await admin.request("DELETE", _sessions(user_id))
    assert none_left.json()["sessions_revoked"] == 0


async def test_sign_out_all_of_an_unknown_account_is_404_and_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    missing = await admin.request("DELETE", _sessions(_GONE))
    assert (missing.status_code, reason(missing)) == (404, "not_found")
    rows = [(r.action, r.outcome, r.reason) for r in await aenv.audit_rows()]
    assert rows == [("sessions.revoke_all", "refused", "not_found")]


async def test_sign_out_all_takes_an_admin_and_the_callers_own_account(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    other = await caller(aenv, "other-admin@example.com")
    done = await admin.request("DELETE", _sessions(other.user_id))
    assert done.status_code == 200, done.text
    assert done.json()["sessions_revoked"] == 1
    after = await other.get("/api/admin/settings")
    assert after.status_code == 401

    own = await admin.request("DELETE", _sessions(admin.user_id))
    assert own.status_code == 200, own.text
    assert own.json()["sessions_revoked"] == 1
    assert (await admin.get("/api/admin/settings")).status_code == 401
    assert await _count(aenv, "SELECT count(*) FROM users WHERE role = 'admin'") == 2


async def test_a_failed_sign_out_rolls_back_with_its_ok_row(
    aenv: AdminEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await caller(aenv)
    user_id = await _target(aenv)
    real = account_writes.sign_out_account

    async def crashing(*args: Any, **kwargs: Any) -> int:
        await real(*args, **kwargs)
        raise RuntimeError("synthetic failure after the delete")

    monkeypatch.setattr(account_writes, "sign_out_account", crashing)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await admin.request("DELETE", _sessions(user_id))
    assert await _count(aenv, "SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 2
    rows = [(r.action, r.outcome, r.reason) for r in await aenv.audit_rows()]
    assert rows == [("sessions.revoke_all", "error", "internal")]


# --- the definers on the maint role -------------------------------------------------------------


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
    connection: Connection, actor: UUID, action: str, target: UUID | None, outcome: str = "ok"
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


_HASH = bytes(range(32))
_CALLS: dict[str, tuple[Any, dict[str, Any]]] = {
    "sessions.revoke_all": (_SIGNOUT, {}),
    "invite.create": (_INVITE, {"h": _HASH, "e": "7 days"}),
}


def _call(connection: Connection, action: str, target: UUID) -> Any:
    statement, params = _CALLS[action]
    return connection.execute(statement, {"t": target, **params}).scalar_one()


@pytest.mark.parametrize("action", sorted(_CALLS))
async def test_each_definer_refuses_without_this_transactions_ok_row(
    aenv: AdminEnv, action: str
) -> None:
    actor = await aenv.env.user("admin@example.com", role="admin")
    user_id = await _target(aenv)
    other = await _target(aenv, "kim@example.com")
    plain = await aenv.env.user("plain@example.com")
    gone = await aenv.env.user("gone-admin@example.com", role="admin")
    await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=gone)
    # The invite's row has no target; the sign-out's names the account.
    target = None if action == "invite.create" else user_id
    wrong_action = "user.disable"

    def refused(connection: Connection, message: str) -> None:
        with pytest.raises(DBAPIError, match=message):
            _call(connection, action, user_id)

    with _maint(aenv) as connection:  # no audit row at all
        refused(connection, "no_audit_row")
    with engine_for(aenv.maint_url) as engine, engine.begin() as connection:  # committed earlier
        connection.execute(text("SET TRANSACTION READ WRITE"))
        _audit(connection, actor, action, target)
    for planted in (
        (actor, wrong_action, target, "ok"),  # another action
        (actor, action, other, "ok"),  # another target (the invite's must have none)
        (actor, action, target, "refused"),  # not an ok row
        (plain, action, target, "ok"),  # the actor is not an admin
        (gone, action, target, "ok"),  # the actor is a disabled admin
    ):
        with _maint(aenv) as connection:
            _audit(connection, *planted)
            refused(connection, "no_audit_row")
    with _maint(aenv) as connection:  # inside a user session
        _audit(connection, actor, action, target)
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(other)})
        refused(connection, "bad_context")
    with _maint(aenv) as connection:  # the matching row: the change goes through
        _audit(connection, actor, action, target)
        assert _call(connection, action, user_id) is not None
    # Every transaction above rolled back.
    assert await _count(aenv, "SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 2
    assert await _count(aenv, "SELECT count(*) FROM invites") == 0


@pytest.mark.parametrize(
    ("code_hash", "expires_in"),
    [
        (bytes(31), "7 days"),
        (bytes(33), "7 days"),
        (None, "7 days"),
        (_HASH, "59 minutes"),
        (_HASH, "30 days 1 second"),
        (_HASH, "-1 day"),
        (_HASH, None),
    ],
)
async def test_the_invite_definer_refuses_a_bad_argument(
    aenv: AdminEnv, code_hash: bytes | None, expires_in: str | None
) -> None:
    actor = await aenv.env.user("admin@example.com", role="admin")
    with _maint(aenv) as connection:
        _audit(connection, actor, "invite.create", None)
        with pytest.raises(DBAPIError, match="bad_argument"):
            connection.execute(_INVITE, {"h": code_hash, "e": expires_in})


async def test_the_invite_definer_takes_the_bounds(aenv: AdminEnv) -> None:
    actor = await aenv.env.user("admin@example.com", role="admin")
    with _maint(aenv) as connection:
        for index, expires_in in enumerate(("1 hour", "30 days")):
            _audit(connection, actor, "invite.create", None)
            connection.execute(_INVITE, {"h": bytes([index]) * 32, "e": expires_in})


@pytest.mark.parametrize("action", sorted(_CALLS))
async def test_the_app_role_may_not_execute_the_definers(
    aenv: AdminEnv, app_database_url: str, action: str
) -> None:
    user_id = await _target(aenv)
    with (
        engine_for(app_database_url) as engine,
        engine.connect() as connection,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        _call(connection, action, user_id)


async def test_maint_reads_and_writes_no_invite_or_session_itself(aenv: AdminEnv) -> None:
    """The definers are the only way: maint has no grant on either table (guard 11)."""
    for sql in (
        "SELECT count(*) FROM invites",
        "INSERT INTO invites (code_hash, expires_at) VALUES ('\\x00', now())",
        "DELETE FROM sessions",
    ):
        with _maint(aenv) as connection, pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(text(sql))
