"""The admin account family (#314, ADR 0002 section 3): an admin's disable, enable and delete
commit in the same transaction as their ``ok`` audit row, and each definer of
``docker/postgres/maint/70-admin-accounts.sql`` refuses unless that transaction already holds
the matching row.

The route tests fail on the old behaviour (the change committed on the app role, the ``ok`` row
afterwards): a failure after the change left it in place with no ``ok`` row. The function tests
call the definers on the maint role directly. Synthetic accounts only.
"""

from collections.abc import Awaitable, Callable, Iterator
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
    caller,
    reason,
)
from jobtools import engine_for
from ownvitals.admin import users as users_module

_MARKER = "synthetic-hash-marker"
_AUDIT = text(
    "INSERT INTO admin_audit (actor_id, actor_email, action, target_user_id, params, outcome, "
    "request_id, ip_hash) VALUES (:actor, 'synthetic-admin@example.com', :action, :target, "
    "CAST('{}' AS jsonb), :outcome, 'synthetic-request', :ip_hash)"
)
_DISABLE = text("SELECT outcome FROM public.admin_account_disable(:t)")
_ENABLE = text("SELECT public.admin_account_enable(:t)")
_DELETE = text("SELECT public.admin_account_delete(:t, :e, NULL, NULL, NULL, NULL)")
CALLS = {"user.disable": _DISABLE, "user.enable": _ENABLE, "user.delete": _DELETE}


class Crash(Exception):
    """A synthetic failure after the account change, inside the admin transaction."""


async def _target(aenv: AdminEnv, email: str = "sam@example.com") -> UUID:
    user_id = await aenv.env.user(email, password_hash=_MARKER)
    await aenv.env.full_login(user_id)
    return user_id


async def _state(aenv: AdminEnv, user_id: UUID) -> tuple[object, object, object]:
    return (
        await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id),
        await aenv.env.scalar("SELECT disabled_at IS NULL FROM users WHERE id = :u", u=user_id),
        await aenv.env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id),
    )


def _crash_after(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real: Callable[..., Awaitable[Any]] = getattr(users_module, name)

    async def crashing(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise Crash("synthetic failure after the change")

    monkeypatch.setattr(users_module, name, crashing)


@pytest.mark.parametrize(
    ("action", "name", "method", "suffix", "body"),
    [
        ("user.disable", "disable_account", "POST", "/disable", None),
        ("user.enable", "enable_account", "POST", "/enable", None),
        ("user.delete", "delete_account", "DELETE", "", {"email": "sam@example.com"}),
    ],
)
async def test_a_failure_after_the_change_rolls_it_back_with_the_ok_row(
    aenv: AdminEnv,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    name: str,
    method: str,
    suffix: str,
    body: dict[str, str] | None,
) -> None:
    admin = await caller(aenv)
    user_id = await _target(aenv)
    if action == "user.enable":
        await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=user_id)
    before = await _state(aenv, user_id)
    _crash_after(monkeypatch, name)
    with pytest.raises(Crash):
        await admin.request(method, f"/api/admin/users/{user_id}{suffix}", body)
    assert await _state(aenv, user_id) == before
    rows = [row for row in await aenv.audit_rows() if row.action == action]
    assert [(row.outcome, row.reason) for row in rows] == [("error", "internal")]
    assert rows[0].target_user_id == user_id


async def test_disable_and_enable_commit_with_their_ok_row(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    user_id = await _target(aenv)
    disabled = await admin.request("POST", f"/api/admin/users/{user_id}/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["sessions_revoked"] == 1
    enabled = await admin.request("POST", f"/api/admin/users/{user_id}/enable")
    assert enabled.status_code == 200, enabled.text
    rows = [(row.action, row.outcome, row.target_user_id) for row in await aenv.audit_rows()]
    assert rows == [("user.disable", "ok", user_id), ("user.enable", "ok", user_id)]


async def test_a_refusal_leaves_one_refused_row_and_no_ok_row(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    user_id = await _target(aenv)
    mismatch = await admin.request(
        "DELETE", f"/api/admin/users/{user_id}", {"email": "other@example.com"}
    )
    assert (mismatch.status_code, reason(mismatch)) == (409, "email_mismatch")
    rows = [(row.outcome, row.reason, row.target_user_id) for row in await aenv.audit_rows()]
    assert rows == [("refused", "email_mismatch", user_id)]
    assert await _state(aenv, user_id) == (1, True, 1)


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
    connection: Connection, actor: UUID, action: str, target: UUID, outcome: str = "ok"
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


def _refused(connection: Connection, action: str, target: UUID, message: str) -> None:
    with pytest.raises(DBAPIError, match=message):
        connection.execute(CALLS[action], {"t": target, "e": "sam@example.com"})


@pytest.mark.parametrize("action", sorted(CALLS))
async def test_each_definer_refuses_without_this_transactions_ok_row(
    aenv: AdminEnv, action: str
) -> None:
    actor = await aenv.env.user("admin@example.com", role="admin")
    user_id = await _target(aenv)
    other = await _target(aenv, "kim@example.com")
    plain = await aenv.env.user("plain@example.com")
    gone = await aenv.env.user("gone-admin@example.com", role="admin")
    await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=gone)
    wrong_action = "user.enable" if action != "user.enable" else "user.disable"
    with _maint(aenv) as connection:  # no audit row at all
        _refused(connection, action, user_id, "no_audit_row")
    with engine_for(aenv.maint_url) as engine, engine.begin() as connection:  # committed earlier
        connection.execute(text("SET TRANSACTION READ WRITE"))
        _audit(connection, actor, action, user_id)
    for planted in (
        (actor, wrong_action, user_id, "ok"),  # another action
        (actor, action, other, "ok"),  # another target
        (actor, action, user_id, "refused"),  # not an ok row
        (plain, action, user_id, "ok"),  # the actor is not an admin
        (gone, action, user_id, "ok"),  # the actor is a disabled admin
    ):
        with _maint(aenv) as connection:
            _audit(connection, *planted)
            _refused(connection, action, user_id, "no_audit_row")
    with _maint(aenv) as connection:  # inside a user session
        _audit(connection, actor, action, user_id)
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(other)})
        _refused(connection, action, user_id, "bad_context")
    with _maint(aenv) as connection:  # the matching row: the change goes through
        _audit(connection, actor, action, user_id)
        outcome = connection.execute(CALLS[action], {"t": user_id, "e": "sam@example.com"})
        assert outcome.scalar_one() == "ok"
    assert await _state(aenv, user_id) == (1, True, 1)  # every transaction above rolled back
    assert (
        await aenv.env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'account_deleted'")
        == 0
    )


@pytest.mark.parametrize("action", sorted(CALLS))
async def test_the_definers_refuse_an_admin_target(aenv: AdminEnv, action: str) -> None:
    actor = await aenv.env.user("admin@example.com", role="admin")
    other = await aenv.env.user("other-admin@example.com", role="admin")
    with _maint(aenv) as connection:
        _audit(connection, actor, action, other)
        result = connection.execute(CALLS[action], {"t": other, "e": "other-admin@example.com"})
        assert result.scalar_one() == "target_is_admin"


@pytest.mark.parametrize("action", sorted(CALLS))
async def test_the_app_role_may_not_execute_the_definers(
    aenv: AdminEnv, app_database_url: str, action: str
) -> None:
    user_id = await _target(aenv)
    with (
        engine_for(app_database_url) as engine,
        engine.connect() as connection,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        connection.execute(CALLS[action], {"t": user_id, "e": "sam@example.com"})


async def test_the_app_role_may_not_execute_the_account_purge(
    aenv: AdminEnv, app_database_url: str
) -> None:
    """#314 moved its one caller into ``admin_account_delete``: no runtime role may call it."""
    user_id = await _target(aenv)
    purge = text("SELECT public.webhook_events_purge_account(:t)")
    for url in (app_database_url, aenv.maint_url):
        with (
            engine_for(url) as engine,
            engine.connect() as connection,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            connection.execute(purge, {"t": user_id})
