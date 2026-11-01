"""The admin area's stored flags (#12): every write leaves an ``ok`` audit row, every refused write
a ``refused`` one and no change; the database can tighten the environment, never loosen it; the
audit row holds the key and what the value means, never the banner's text."""

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    reason,
)
from authtools import running_env
from jobtools import engine_for


async def _stored(aenv: AdminEnv) -> dict[str, Any]:
    rows = await aenv.superuser("SELECT key, value FROM app_settings")
    return {r.key: r.value for r in rows.all()}


def _flag(body: dict[str, Any], key: str) -> dict[str, Any]:
    (found,) = [f for f in body["flags"] if f["key"] == key]
    return found


async def test_the_view_lists_every_flag_from_the_environment_at_first(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    response = await admin.get("/api/admin/settings")
    assert response.status_code == 200
    body = response.json()
    assert [f["key"] for f in body["flags"]] == [
        "signup_mode",
        "experimental_findings",
        "ai_insights",
        "banner",
    ]
    assert _flag(body, "signup_mode")["effective"] == "invite"
    assert _flag(body, "experimental_findings")["effective"] == "off"
    assert _flag(body, "ai_insights")["effective"] == "off"
    assert _flag(body, "banner")["effective"] is None
    assert {f["source"] for f in body["flags"]} == {"environment"}
    ((row,),) = [await aenv.audit_rows()]
    assert (row.action, row.outcome) == ("setting.read", "ok")


@pytest.mark.parametrize(
    ("key", "value", "params"),
    [
        ("signup_mode", "closed", {"key": "signup_mode", "value": "closed"}),
        ("signup_mode", "invite", {"key": "signup_mode", "value": "invite"}),
        ("experimental_findings", "off", {"key": "experimental_findings", "value": "off"}),
        ("experimental_findings", "on", {"key": "experimental_findings", "value": "on"}),
        ("ai_insights", "on", {"key": "ai_insights", "value": "on"}),
        (
            "banner",
            {"text": "Maintenance tonight", "level": "info"},
            {"key": "banner", "level": "info", "text_length": 19},
        ),
    ],
)
async def test_every_write_leaves_an_ok_row(
    aenv: AdminEnv, key: str, value: Any, params: dict[str, Any]
) -> None:
    admin = await caller(aenv)
    response = await admin.put(f"/api/admin/settings/{key}", {"value": value})
    assert response.status_code == 200, response.text
    assert response.json()["stored"] == value
    assert (await _stored(aenv))[key] == value
    (row,) = await aenv.audit_rows()
    assert (row.action, row.outcome, row.reason) == ("setting.set", "ok", None)
    assert (row.target_kind, row.target_id, row.params) == ("setting", key, params)
    assert row.actor_id == admin.user_id


@pytest.mark.parametrize(
    ("key", "value", "refusal"),
    [
        ("signup_mode", "open", "environment_only"),
        ("signup_mode", "public", "invalid_value"),
        ("experimental_findings", True, "invalid_value"),
        ("ai_insights", "granted", "invalid_value"),
        ("banner", {"text": "", "level": "info"}, "invalid_value"),
        ("banner", {"text": "x" * 281, "level": "info"}, "invalid_value"),
        ("banner", {"text": "abc\u202edef", "level": "info"}, "invalid_value"),
        ("banner", {"text": "ok", "level": "critical"}, "invalid_value"),
        ("banner", {"text": "ok", "level": "info", "extra": 1}, "invalid_value"),
        ("theme", "dark", "unknown_key"),
        ("song_picking", "on", "unknown_key"),
    ],
)
async def test_a_refused_write_changes_nothing_and_leaves_a_refused_row(
    aenv: AdminEnv, key: str, value: Any, refusal: str
) -> None:
    admin = await caller(aenv)
    response = await admin.put(f"/api/admin/settings/{key}", {"value": value})
    assert (response.status_code, reason(response)) == (422, refusal)
    assert await _stored(aenv) == {}
    (row,) = await aenv.audit_rows()
    assert (row.action, row.outcome, row.reason) == ("setting.set", "refused", refusal)
    assert row.params == {"key": key}  # the refused value is never recorded


async def test_the_banner_text_is_never_in_the_audit_log(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    text_ = "synthetic-banner-text-not-for-the-log"
    await admin.put("/api/admin/settings/banner", {"value": {"text": text_, "level": "warning"}})
    await admin.put("/api/admin/settings/banner", {"value": {"text": text_, "level": "loud"}})
    rows = await aenv.audit_rows()
    assert len(rows) == 2
    assert all(text_ not in repr(tuple(r)) for r in rows)


async def test_a_stored_flag_tightens_but_never_loosens_the_environment(
    aenv: AdminEnv, app_database_url: str
) -> None:
    admin = await caller(aenv)
    await admin.put("/api/admin/settings/experimental_findings", {"value": "on"})
    await admin.put("/api/admin/settings/signup_mode", {"value": "invite"})
    body = (await admin.get("/api/admin/settings")).json()
    # The environment has the switch off: a stored ``on`` does not turn it on.
    assert _flag(body, "experimental_findings")["effective"] == "off"
    assert _flag(body, "experimental_findings")["source"] == "environment"
    # Another environment over the same database: sign-up closed, AI insights on.
    async with running_env(
        app_database_url,
        signup_mode="closed",
        ai_insights=True,
        maint_database_url=aenv.maint_url,
        database_maint_password=make_url(aenv.maint_url).password,
    ) as running:
        other = AdminEnv(running, aenv.maint_url)
        admin = await caller(other, "other@example.com")
        body = (await admin.get("/api/admin/settings")).json()
        # A stored ``invite`` does not open what the environment closed.
        assert _flag(body, "signup_mode")["effective"] == "closed"
        assert _flag(body, "ai_insights")["effective"] == "on"
        await admin.put("/api/admin/settings/ai_insights", {"value": "off"})
        body = (await admin.get("/api/admin/settings")).json()
        assert (_flag(body, "ai_insights")["effective"], _flag(body, "ai_insights")["source"]) == (
            "off",
            "database",
        )
        await other.superuser(
            "UPDATE app_settings SET value = '\"bogus\"' WHERE key = 'ai_insights'"
        )
        body = (await admin.get("/api/admin/settings")).json()
        assert _flag(body, "ai_insights")["effective"] == "off"  # invalid: the stricter default
        assert _flag(body, "ai_insights")["stored_valid"] is False


async def test_clearing_a_flag_stores_null_and_a_second_clear_is_404(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    await admin.put("/api/admin/settings/signup_mode", {"value": "closed"})
    response = await admin.delete("/api/admin/settings/signup_mode")
    assert response.status_code == 200
    assert response.json()["effective"] == "invite"
    assert (await _stored(aenv))["signup_mode"] is None
    response = await admin.delete("/api/admin/settings/signup_mode")
    assert (response.status_code, reason(response)) == (404, "not_found")
    response = await admin.delete("/api/admin/settings/theme")
    assert (response.status_code, reason(response)) == (422, "unknown_key")
    rows = await aenv.audit_rows()
    assert [(r.action, r.outcome, r.reason) for r in rows] == [
        ("setting.set", "ok", None),
        ("setting.clear", "ok", None),
        ("setting.clear", "refused", "not_found"),
        ("setting.clear", "refused", "unknown_key"),
    ]


async def test_the_public_settings_show_the_stricter_mode_and_the_banner(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    await admin.put("/api/admin/settings/signup_mode", {"value": "closed"})
    await admin.put("/api/admin/settings/banner", {"value": {"text": "Hello", "level": "info"}})
    response = await aenv.client.get("/api/settings/public")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "banner": {"text": "Hello", "level": "info"},
        "signup_mode": "closed",
    }


async def test_the_app_role_can_only_read_the_flags(aenv: AdminEnv, app_database_url: str) -> None:
    with engine_for(app_database_url) as engine, engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM app_settings")).scalar() == 0
        for statement in (
            "INSERT INTO app_settings (key, value) VALUES ('signup_mode', '\"invite\"')",
            "UPDATE app_settings SET value = 'null'",
            "DELETE FROM app_settings",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                connection.execute(text(statement))
            connection.rollback()
        with pytest.raises(DBAPIError, match="permission denied"):
            connection.execute(text("SELECT count(*) FROM admin_audit"))
    del aenv
