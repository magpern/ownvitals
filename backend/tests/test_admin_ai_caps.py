"""The AI monthly caps in the admin area (#528): a stored cap only lowers the environment's, the
per-user cap stays at or below the global one, every write needs the step-up and leaves an audit
row with the key and the amount, a refused write changes nothing, and clearing a cap returns to
the environment's. Synthetic amounts and accounts only."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import make_url

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    reason,
)
from authtools import running_env

USER, GLOBAL = "ai_user_monthly_cap_microusd", "ai_global_monthly_cap_microusd"
ENV_USER, ENV_GLOBAL = 10_000_000, 100_000_000


@asynccontextmanager
async def capped(
    aenv: AdminEnv, app_database_url: str, user: int = ENV_USER, global_: int = ENV_GLOBAL
) -> AsyncIterator[AdminEnv]:
    """The same database under an environment with explicit caps (10 and 100 USD unless given)."""
    async with running_env(
        app_database_url,
        ai_user_monthly_cap_microusd=user,
        ai_global_monthly_cap_microusd=global_,
        maint_database_url=aenv.maint_url,
        database_maint_password=make_url(aenv.maint_url).password,
    ) as running:
        yield AdminEnv(running, aenv.maint_url)


def _flag(body: dict[str, Any], key: str) -> dict[str, Any]:
    (found,) = [f for f in body["flags"] if f["key"] == key]
    return found


async def _stored(aenv: AdminEnv) -> dict[str, Any]:
    rows = await aenv.superuser("SELECT flag, value FROM app_settings")
    return {r.flag: r.value for r in rows.all()}


async def test_the_caps_are_listed_with_the_environment_s_values(
    aenv: AdminEnv, app_database_url: str
) -> None:
    async with capped(aenv, app_database_url) as env:
        admin = await caller(env)
        body = (await admin.get("/api/admin/settings")).json()
    for key, value in ((USER, ENV_USER), (GLOBAL, ENV_GLOBAL)):
        flag = _flag(body, key)
        assert (flag["effective"], flag["environment"], flag["source"]) == (
            value,
            value,
            "environment",
        )
        assert (flag["stored"], flag["stored_valid"]) == (None, True)


async def test_a_lower_cap_is_stored_and_audited_with_its_amount(
    aenv: AdminEnv, app_database_url: str
) -> None:
    async with capped(aenv, app_database_url) as env:
        admin = await caller(env)
        response = await admin.put(f"/api/admin/settings/{GLOBAL}", {"value": 50_000_000})
        assert response.status_code == 200, response.text
        assert (response.json()["effective"], response.json()["source"]) == (50_000_000, "database")
        response = await admin.put(f"/api/admin/settings/{USER}", {"value": 5_000_000})
        assert response.status_code == 200, response.text
        assert response.json()["effective"] == 5_000_000
        assert await _stored(env) == {GLOBAL: 50_000_000, USER: 5_000_000}
        rows = await env.audit_rows()
    assert [(r.action, r.outcome, r.target_id, r.params) for r in rows] == [
        ("setting.set", "ok", GLOBAL, {"key": GLOBAL, "amount_microusd": 50_000_000}),
        ("setting.set", "ok", USER, {"key": USER, "amount_microusd": 5_000_000}),
    ]
    assert all(r.actor_id == admin.user_id for r in rows)


async def test_a_cap_above_the_environment_or_not_an_integer_is_refused(
    aenv: AdminEnv, app_database_url: str
) -> None:
    async with capped(aenv, app_database_url) as env:
        admin = await caller(env)
        for key, value, refusal in (
            (USER, ENV_USER + 1, "above_environment"),
            (GLOBAL, ENV_GLOBAL + 1, "above_environment"),
            (USER, 5.5, "invalid_value"),
            (USER, "5000000", "invalid_value"),
            (GLOBAL, -1, "invalid_value"),
            (GLOBAL, True, "invalid_value"),
        ):
            response = await admin.put(f"/api/admin/settings/{key}", {"value": value})
            assert (response.status_code, reason(response)) == (422, refusal), (key, value)
        assert await _stored(env) == {}
        rows = await env.audit_rows()
    assert len(rows) == 6
    assert all((r.outcome, r.params) == ("refused", {"key": r.target_id}) for r in rows)


async def test_the_per_user_cap_may_not_end_above_the_global_one(
    aenv: AdminEnv, app_database_url: str
) -> None:
    async with capped(aenv, app_database_url) as env:
        admin = await caller(env)
        # The environment's per-user cap (10 USD) is above a global cap of 5 USD.
        response = await admin.put(f"/api/admin/settings/{GLOBAL}", {"value": 5_000_000})
        assert (response.status_code, reason(response)) == (422, "user_above_global")
        assert (await admin.put(f"/api/admin/settings/{USER}", {"value": 4_000_000})).is_success
        assert (await admin.put(f"/api/admin/settings/{GLOBAL}", {"value": 5_000_000})).is_success
        response = await admin.put(f"/api/admin/settings/{USER}", {"value": 6_000_000})
        assert (response.status_code, reason(response)) == (422, "user_above_global")
        response = await admin.put(f"/api/admin/settings/{GLOBAL}", {"value": 3_999_999})
        assert (response.status_code, reason(response)) == (422, "user_above_global")
        assert await _stored(env) == {USER: 4_000_000, GLOBAL: 5_000_000}
        # Clearing the per-user cap returns to the environment's, held at the global cap.
        response = await admin.delete(f"/api/admin/settings/{USER}")
        assert response.status_code == 200
        assert response.json()["effective"] == 5_000_000
        body = (await admin.get("/api/admin/settings")).json()
        assert _flag(body, GLOBAL)["effective"] == 5_000_000
        # Clearing the global cap returns both to the environment's.
        assert (await admin.delete(f"/api/admin/settings/{GLOBAL}")).status_code == 200
        body = (await admin.get("/api/admin/settings")).json()
        assert (_flag(body, USER)["effective"], _flag(body, GLOBAL)["effective"]) == (
            ENV_USER,
            ENV_GLOBAL,
        )
        rows = [r for r in await env.audit_rows() if r.action != "setting.read"]
    assert [(r.action, r.outcome, r.reason) for r in rows] == [
        ("setting.set", "refused", "user_above_global"),
        ("setting.set", "ok", None),
        ("setting.set", "ok", None),
        ("setting.set", "refused", "user_above_global"),
        ("setting.set", "refused", "user_above_global"),
        ("setting.clear", "ok", None),
        ("setting.clear", "ok", None),
    ]


async def test_a_cap_write_needs_the_step_up(aenv: AdminEnv, app_database_url: str) -> None:
    async with capped(aenv, app_database_url) as env:
        admin = await caller(env, stepup=False)
        response = await admin.put(f"/api/admin/settings/{USER}", {"value": 1_000_000})
        assert (response.status_code, reason(response)) == (403, "step_up_required")
        response = await admin.delete(f"/api/admin/settings/{USER}")
        assert (response.status_code, reason(response)) == (403, "step_up_required")
        assert await _stored(env) == {}


async def test_with_the_environment_at_zero_only_zero_can_be_stored(
    aenv: AdminEnv, app_database_url: str
) -> None:
    """An environment with both caps set to 0 (no request can be reserved) keeps that meaning."""
    async with capped(aenv, app_database_url, user=0, global_=0) as env:
        admin = await caller(env)
        for key in (GLOBAL, USER):
            response = await admin.put(f"/api/admin/settings/{key}", {"value": 1})
            assert (response.status_code, reason(response)) == (422, "above_environment")
        response = await admin.put(f"/api/admin/settings/{GLOBAL}", {"value": 0})
        assert response.status_code == 200
        assert response.json()["effective"] == 0


async def test_an_invalid_stored_cap_counts_as_zero(aenv: AdminEnv, app_database_url: str) -> None:
    async with capped(aenv, app_database_url) as env:
        admin = await caller(env)
        await env.superuser(
            "INSERT INTO app_settings (flag, value) VALUES (:k, '\"lots\"')", k=GLOBAL
        )
        body = (await admin.get("/api/admin/settings")).json()
    assert (_flag(body, GLOBAL)["effective"], _flag(body, GLOBAL)["stored_valid"]) == (0, False)
    assert _flag(body, USER)["effective"] == 0
