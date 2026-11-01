"""Admin writes and the step-up window (#140).

A ``stepup_at`` after the database ``now`` is refused. ``stepup_within`` keeps
``min(window, stepup_window)``, so a configured window shorter than five minutes refuses a
step-up the five-minute admin cap alone would allow. Accounts and values here are synthetic.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import make_url

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    Caller,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    reason,
    truncate_admin,
)
from authtools import running_env
from jobtools import admin_url

_SETTING = "/api/admin/settings/signup_mode"
_BODY = {"value": "closed"}


@asynccontextmanager
async def _window(app_database_url: str, maint_url: str, minutes: int) -> AsyncIterator[AdminEnv]:
    truncate_admin(admin_url())
    async with running_env(
        app_database_url,
        maint_database_url=maint_url,
        database_maint_password=make_url(maint_url).password,
        stepup_window_minutes=minutes,
    ) as running:
        yield AdminEnv(running, maint_url)


async def _age(aenv: AdminEnv, who: Caller, interval: str) -> None:
    await aenv.env.run(
        "UPDATE sessions SET stepup_at = now() - CAST(:a AS interval) WHERE user_id = :u",
        a=interval,
        u=who.user_id,
    )


async def test_a_future_stepup_at_refuses_an_admin_write(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    allowed = await admin.put(_SETTING, _BODY)
    assert allowed.status_code == 200, allowed.text
    await aenv.env.run(
        "UPDATE sessions SET stepup_at = now() + CAST(:a AS interval) WHERE user_id = :u",
        a="1 minute",
        u=admin.user_id,
    )
    refused = await admin.put(_SETTING, _BODY)
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")


async def test_a_shorter_configured_window_refuses_an_admin_write(
    admin_schema: str, app_database_url: str
) -> None:
    """Two minutes configured, four minutes since the step-up: inside five minutes, outside
    the setting. A mutant that drops ``min`` and uses the five-minute cap allows the write."""
    async with _window(app_database_url, admin_schema, 2) as aenv:
        admin = await caller(aenv)
        await _age(aenv, admin, "1 minute")
        allowed = await admin.put(_SETTING, _BODY)
        assert allowed.status_code == 200, allowed.text
        await _age(aenv, admin, "4 minutes")
        refused = await admin.put(_SETTING, _BODY)
        assert (refused.status_code, reason(refused)) == (403, "step_up_required")
