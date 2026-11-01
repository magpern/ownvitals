"""The flags as a request sees them (``ownvitals.flags.effective``), read from ``app_settings``
on the app role: the stricter of the environment and the stored value; a cleared flag (the JSON
``null``) reads as no row; an invalid stored value falls back to the stricter default."""

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from jobtools import admin_url, engine_for
from ownvitals.flags import effective

ABSENT: Any = object()


def _store(key: str, value: Any) -> None:
    with engine_for(admin_url()) as engine, engine.begin() as connection:
        connection.execute(text("DELETE FROM app_settings"))
        if value is not ABSENT:
            connection.execute(
                text("INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"),
                {"k": key, "v": json.dumps(value)},
            )


@pytest.mark.parametrize(
    ("environment", "stored", "expected"),
    [
        ("invite", ABSENT, "invite"),
        ("invite", None, "invite"),  # cleared
        ("invite", "closed", "closed"),
        ("closed", "invite", "closed"),
        ("invite", "open", "invite"),  # invalid: the stricter default
    ],
)
async def test_signup_mode(env: Env, environment: Any, stored: Any, expected: str) -> None:
    _store("signup_mode", stored)
    async with env.db() as db:
        assert await effective.signup_mode(db, environment) == expected


@pytest.mark.parametrize("key", ["experimental_findings", "ai_insights"])
@pytest.mark.parametrize(
    ("environment", "stored", "expected"),
    [
        (False, ABSENT, False),
        (False, "on", False),
        (True, ABSENT, True),
        (True, None, True),
        (True, "off", False),
        (True, "on", True),
        (True, "yes", False),
    ],
)
async def test_switches(env: Env, key: str, environment: bool, stored: Any, expected: bool) -> None:
    _store(key, stored)
    settings = env.settings.model_copy(update={key: environment})
    reader = effective.ai_insights if key == "ai_insights" else effective.experimental_findings
    async with env.db() as db:
        assert await reader(db, settings) is expected


async def test_banner(env: Env) -> None:
    _store("banner", {"text": "Hello", "level": "warning"})
    async with env.db() as db:
        shown = await effective.banner(db)
    assert shown is not None
    assert (shown.text, shown.level) == ("Hello", "warning")
    _store("banner", {"text": "a\u202eb", "level": "info"})
    async with env.db() as db:
        assert await effective.banner(db) is None


async def test_the_table_refuses_an_unknown_key(env: Env) -> None:
    del env
    with pytest.raises(IntegrityError, match="app_settings_key"):
        _store("theme", "dark")


async def test_the_public_settings_need_no_sign_in(env: Env) -> None:
    _store("signup_mode", "closed")
    response = await env.client.get("/api/settings/public")
    assert response.status_code == 200
    assert response.json() == {"banner": None, "signup_mode": "closed"}
