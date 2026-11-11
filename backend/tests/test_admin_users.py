"""The admin Users view (#127): account helpers behind guard 8, step-up and an audit row.

Synthetic accounts only. The address prefix is not stored. A password hash, session token and
user agent never appear in a response.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import SecretStr

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    reason,
)
from jobtools import admin_url
from ownvitals.admin import user_revoke
from ownvitals.admin import users as users_module
from ownvitals.admin.routes import admin_area_routes, calls
from ownvitals.auth.admin_accounts import AccountSummary, account_emails
from ownvitals.auth.deps import require_admin_stepup
from ownvitals.config import Settings
from ownvitals.web import create_app
from withingsschema import new_app

_MARKER = "synthetic-hash-marker"
_GONE = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _app() -> FastAPI:
    return create_app(Settings(environment="test", app_secret_key=SecretStr("k" * 40)))  # pyright: ignore[reportCallIssue]


def test_writes_depend_on_require_admin_stepup() -> None:
    app = _app()
    writes = {
        ("POST", "/api/admin/users/{user_id}/disable"),
        ("POST", "/api/admin/users/{user_id}/enable"),
        ("DELETE", "/api/admin/users/{user_id}"),
    }
    seen: set[tuple[str, str]] = set()
    for path, route in admin_area_routes(app.routes):
        if not isinstance(route, APIRoute) or not path.startswith("/api/admin/users"):
            continue
        for method in route.methods or set():
            keyed = (method, path)
            if method in ("GET", "HEAD"):
                assert require_admin_stepup not in calls(route.dependant), keyed
            elif keyed in writes:
                assert require_admin_stepup in calls(route.dependant), keyed
                seen.add(keyed)
    assert seen == writes


def test_an_account_summary_repr_omits_the_address() -> None:
    row = AccountSummary(
        id=_GONE,
        email="sam@example.com",
        email_normalized="sam@example.com",
        role="user",
        disabled_at=None,
        created_at=row_time(),
        second_factor="setup_pending",
        last_sign_in=None,
        sessions=0,
    )
    assert "sam@example.com" not in repr(row)


def row_time() -> datetime:
    return datetime(2026, 10, 29, tzinfo=UTC)


async def test_account_emails_skips_an_unknown_id_and_caps_the_lookup(aenv: AdminEnv) -> None:
    user_id = await aenv.env.user("sam@example.com")
    found = await account_emails(aenv.env.factory, [user_id, _GONE])
    assert found == {user_id: "sam@example.com"}
    too_many = [uuid.uuid4() for _ in range(101)]
    with pytest.raises(ValueError, match="at most 100"):
        await account_emails(aenv.env.factory, too_many)


async def _user(aenv: AdminEnv, email: str, *, totp: bool = False) -> uuid.UUID:
    user_id = await aenv.env.user(email, password_hash=_MARKER)
    if totp:
        await aenv.env.full_login(user_id)
        await aenv.env.run(
            "INSERT INTO auth_events (kind, user_id) VALUES ('sign_in', :u)", u=user_id
        )
    return user_id


async def test_the_list_shows_accounts_and_stores_only_the_prefix_length(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    sam = await _user(aenv, "sam@example.com", totp=True)
    kim = await _user(aenv, "kim@example.com")
    response = await admin.get("/api/admin/users?q=sam@&limit=50")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [row["email"] for row in body["users"]] == ["sam@example.com"]
    (row,) = body["users"]
    assert row["id"] == str(sam)
    assert row["role"] == "user"
    assert row["second_factor"] == "on"
    assert row["sessions"] == 1
    assert row["last_sign_in"] is not None
    assert row["disabled_at"] is None
    assert _MARKER not in response.text
    assert "user_agent" not in response.text
    assert str(kim) not in response.text
    (audit,) = await aenv.audit_rows()
    assert audit.action == "user.list"
    assert audit.params == {"limit": 50, "offset": 0, "q_length": 4}
    assert "sam@" not in str(audit.params)


async def test_a_percent_prefix_is_literal_and_setup_pending_has_no_sign_in(
    aenv: AdminEnv,
) -> None:
    admin = await caller(aenv)
    await _user(aenv, "kim@example.com")
    response = await admin.get("/api/admin/users?q=%")
    assert response.status_code == 200, response.text
    assert response.json()["users"] == []
    one = await admin.get("/api/admin/users?q=kim@example.com")
    assert one.status_code == 200
    (row,) = one.json()["users"]
    assert row["second_factor"] == "setup_pending"
    assert row["last_sign_in"] is None
    assert row["sessions"] == 0


async def test_find_disable_enable_and_a_missing_step_up_changes_nothing(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    user_id = await _user(aenv, "sam@example.com", totp=True)
    found = await admin.get(f"/api/admin/users/{user_id}")
    assert found.status_code == 200, found.text
    assert found.json()["email"] == "sam@example.com"
    assert found.json()["second_factor"] == "on"

    cold = await caller(aenv, "cold@example.com", stepup=False)
    refused = await cold.request("POST", f"/api/admin/users/{user_id}/disable")
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    assert await aenv.env.scalar("SELECT disabled_at IS NULL FROM users WHERE id = :u", u=user_id)

    disabled = await admin.request("POST", f"/api/admin/users/{user_id}/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["sessions_revoked"] == 1
    assert disabled.json()["disabled_at"] is not None
    assert await aenv.env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 0

    enabled = await admin.request("POST", f"/api/admin/users/{user_id}/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["disabled_at"] is None
    assert await aenv.env.scalar("SELECT disabled_at IS NULL FROM users WHERE id = :u", u=user_id)

    missing = await admin.request("POST", f"/api/admin/users/{_GONE}/disable")
    assert (missing.status_code, reason(missing)) == (404, "not_found")
    own = await admin.request("POST", f"/api/admin/users/{admin.user_id}/disable")
    assert (own.status_code, reason(own)) == (409, "target_is_admin")
    reasons = [row.reason for row in await aenv.audit_rows() if row.action == "user.disable"]
    assert "target_is_admin" in reasons
    assert "not_found" in reasons
    assert any(
        row.action == "user.disable" and row.outcome == "ok" for row in await aenv.audit_rows()
    )


async def test_delete_uses_the_account_delete_path_and_refuses_an_admin(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    user_id = await _user(aenv, "sam@example.com", totp=True)
    mismatch = await admin.request(
        "DELETE", f"/api/admin/users/{user_id}", {"email": "other@example.com"}
    )
    assert (mismatch.status_code, reason(mismatch)) == (409, "email_mismatch")
    assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 1
    assert (
        await aenv.env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'account_deleted'")
        == 0
    )

    removed = await admin.request(
        "DELETE", f"/api/admin/users/{user_id}", {"email": "SAM@example.com"}
    )
    assert removed.status_code == 204, removed.text
    assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    assert await aenv.env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 0
    assert (
        await aenv.env.scalar("SELECT user_id FROM auth_events WHERE kind = 'account_deleted'")
        is None
    )
    deleted = [
        row
        for row in await aenv.audit_rows()
        if row.action == "user.delete" and row.outcome == "ok"
    ]
    assert len(deleted) == 1
    assert deleted[0].params == {"revoke": "none"}  # #365: no live connection, nothing queued
    assert deleted[0].target_user_id is None
    assert deleted[0].target_kind == "user"
    assert deleted[0].target_id == str(user_id)
    assert "sam@" not in str(deleted[0].params)

    again = await admin.request(
        "DELETE", f"/api/admin/users/{user_id}", {"email": "sam@example.com"}
    )
    assert (again.status_code, reason(again)) == (404, "not_found")
    own = await admin.request(
        "DELETE", f"/api/admin/users/{admin.user_id}", {"email": "admin@example.com"}
    )
    assert (own.status_code, reason(own)) == (409, "target_is_admin")
    assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=admin.user_id) == 1


async def test_enable_refuses_an_admin_like_disable_and_delete(aenv: AdminEnv) -> None:
    """#277: enable used to clear ``disabled_at`` for an admin; the admin role stays with the
    operator CLI, so enable refuses an admin (409) and changes nothing."""
    admin = await caller(aenv)
    other = await caller(aenv, "other-admin@example.com")
    await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=other.user_id)
    refused = await admin.request("POST", f"/api/admin/users/{other.user_id}/enable")
    assert (refused.status_code, reason(refused)) == (409, "target_is_admin")
    assert await aenv.env.scalar(
        "SELECT disabled_at IS NOT NULL FROM users WHERE id = :u", u=other.user_id
    )
    rows = [row for row in await aenv.audit_rows() if row.action == "user.enable"]
    assert [(row.outcome, row.reason) for row in rows] == [("refused", "target_is_admin")]


async def test_a_lookup_miss_on_read_leaves_no_ok_row(
    aenv: AdminEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#277: the address lookup missing after ``find_account`` (the account went in between)
    rolled back nothing: ``user.read`` had already committed ``ok``. Now the miss is inside the
    audited transaction, so the only row is the refusal."""
    admin = await caller(aenv)
    user_id = await _user(aenv, "sam@example.com")

    async def gone(factory: object, ids: object) -> dict[uuid.UUID, str]:
        return {}

    monkeypatch.setattr(users_module, "account_emails", gone)
    response = await admin.get(f"/api/admin/users/{user_id}")
    assert (response.status_code, reason(response)) == (404, "not_found")
    rows = [row for row in await aenv.audit_rows() if row.action == "user.read"]
    assert [(row.outcome, row.reason) for row in rows] == [("refused", "not_found")]


_EVENT_KEYS = ("synthetic-admin-delete-target", "synthetic-admin-delete-kept")
_TARGET_USERID, _KEPT_USERID = 4101, 4102  # synthetic Withings user ids


async def _link(aenv: AdminEnv, owner: uuid.UUID, app_id: int, userid: int) -> None:
    """A synthetic Withings connection row, linked to ``app_id``/``userid``, and one webhook
    event for that pair (no measurement, no credential)."""
    public = f'{{"app_id": {app_id}, "withings_userid": {userid}, "scopes": ["user.metrics"]}}'
    await aenv.superuser(
        "INSERT INTO connections (owner_id, provider, status, public) "
        "VALUES (:o, 'withings', 'needs_reconnect', CAST(:p AS jsonb))",
        o=owner,
        p=public,
    )
    await aenv.superuser(
        "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
        "dedupe_key) VALUES (:a, :u, 1, 1, 2, :k)",
        a=app_id,
        u=userid,
        k=_EVENT_KEYS[0] if userid == _TARGET_USERID else _EVENT_KEYS[1],
    )


async def _event_userids(aenv: AdminEnv) -> set[int]:
    rows = await aenv.superuser(
        "SELECT withings_userid FROM webhook_events WHERE dedupe_key = ANY(:k)",
        k=list(_EVENT_KEYS),
    )
    return {int(value) for value in rows.scalars()}


async def test_delete_purges_the_targets_webhook_events_as_the_users_own_delete_does(
    aenv: AdminEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#362: webhook events have no ``owner_id`` and do not cascade with the account, so the
    admin delete purges them first (``webhook_events_purge_account``, in its system session),
    as ``auth.purge.delete_account`` does. A refused delete removes none, and another user's
    events stay. No worker runs here, so the revoke of the live row (#365) is recorded as
    failed after a short wait."""
    monkeypatch.setattr(user_revoke, "ADMIN_REVOKE_WAIT_SECONDS", 0.2)
    admin = await caller(aenv)
    target = await _user(aenv, "sam@example.com", totp=True)
    kept = await _user(aenv, "kim@example.com")
    app_id = new_app(admin_url())
    try:
        await _link(aenv, target, app_id, _TARGET_USERID)
        await _link(aenv, kept, app_id, _KEPT_USERID)
        assert await _event_userids(aenv) == {_TARGET_USERID, _KEPT_USERID}

        mismatch = await admin.request(
            "DELETE", f"/api/admin/users/{target}", {"email": "other@example.com"}
        )
        assert (mismatch.status_code, reason(mismatch)) == (409, "email_mismatch")
        assert await _event_userids(aenv) == {_TARGET_USERID, _KEPT_USERID}

        removed = await admin.request(
            "DELETE", f"/api/admin/users/{target}", {"email": "sam@example.com"}
        )
        assert removed.status_code == 204, removed.text
        assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=target) == 0
        assert await _event_userids(aenv) == {_KEPT_USERID}
        deleted = [row.params for row in await aenv.audit_rows() if row.action == "user.delete"]
        assert deleted[-1] == {"revoke": "failed"}
        owners = await aenv.superuser(
            "SELECT owner_id FROM connections WHERE app_id = :a", a=app_id
        )
        assert list(owners.scalars()) == [kept]
    finally:
        await aenv.superuser(
            "DELETE FROM webhook_events WHERE dedupe_key = ANY(:k)", k=list(_EVENT_KEYS)
        )
        await aenv.superuser("DELETE FROM connections WHERE app_id = :a", a=app_id)
        await aenv.superuser("DELETE FROM withings_apps WHERE id = :a", a=app_id)
