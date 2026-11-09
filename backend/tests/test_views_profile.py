"""``GET`` and ``PUT /api/profile`` (issue #203): the analysis profile, display and notification
choices of the signed-in user on ``user_profiles``. Synthetic accounts only.

Covers: defaults before anything is stored, the round trip, server-side validation (nothing
invalid reaches a column), isolation between users under forced RLS (each request runs as its own
``UserContext``), the experimental switch, and the recompute that a change of sex or day
assignment queues.
"""

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from flows import Browser
from ownvitals.views.deps import view_now
from ownvitals.views.profile_models import DEFAULTS, ProfileIn, moves_the_analysis
from test_views_findings import store_flag
from viewtools import (
    NOW,
    _conn,
    connected_with_streams,
    env_fixture,  # noqa: F401 - fixture
    user,
)

SYNTHETIC = True
PROFILE = "/api/profile"


@pytest.fixture(name="owner_env")
async def owner_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url, experimental_findings=True) as running:
        running.app.dependency_overrides[view_now] = lambda: NOW
        yield running


def body(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(DEFAULTS)
    document.update(changes)
    return document


def rows() -> list[Any]:
    with _conn() as engine, engine.begin() as conn:
        return list(conn.execute(text("SELECT owner_id, birth_year, sex FROM user_profiles")))


def queued_kinds() -> list[str]:
    with _conn() as engine, engine.begin() as conn:
        return [r[0] for r in conn.execute(text("SELECT kind FROM jobs ORDER BY created_at"))]


async def test_a_new_account_reads_the_defaults_and_no_row_is_made(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    response = await browser.get(PROFILE)
    assert response.status_code == 200, response.text
    out = response.json()
    assert {k: out[k] for k in DEFAULTS} == DEFAULTS
    assert (out["experimental_available"], out["ai_available"]) == (False, False)
    assert rows() == []


async def test_a_profile_is_stored_and_read_back(env: Env) -> None:
    browser, owner = await user(env, "a@example.com")
    saved = body(
        birth_year=1982,
        sex="female",
        theme="dark",
        weight_unit="st",
        temperature_unit="f",
        datetime_format="iso",
        chart_range="1y",
        overview_order=["heart", "body"],
        show_unsupported=False,
        notify_email=True,
        notify_tiers=["device", "system"],
    )
    response = await browser.request("PUT", PROFILE, saved)
    assert response.status_code == 200, response.text
    again = (await browser.get(PROFILE)).json()
    assert {k: again[k] for k in saved} == saved
    assert [tuple(r) for r in rows()] == [(owner, 1982, "female")]
    # A second save replaces the one row.
    await browser.request("PUT", PROFILE, {**saved, "theme": "light", "birth_year": None})
    assert len(rows()) == 1
    assert (await browser.get(PROFILE)).json()["theme"] == "light"


async def test_each_user_reads_and_writes_only_their_own_row(env: Env) -> None:
    first, first_id = await user(env, "a@example.com")
    second, second_id = await user(env, "b@example.com")
    await first.request("PUT", PROFILE, body(birth_year=1970, sex="male", theme="dark"))
    assert (await second.get(PROFILE)).json()["theme"] == "system"
    await second.request("PUT", PROFILE, body(birth_year=1990))
    assert (await first.get(PROFILE)).json()["birth_year"] == 1970
    assert {(r[0], r[1]) for r in rows()} == {(first_id, 1970), (second_id, 1990)}


async def test_the_owner_id_cannot_be_chosen_by_the_client(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    _, other = await user(env, "b@example.com")
    response = await browser.request("PUT", PROFILE, {**body(), "owner_id": str(other)})
    assert response.status_code == 422
    assert rows() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"birth_year": 1899},
        {"birth_year": 2101},
        {"birth_year": 2027},  # after the fixed clock's year
        {"birth_year": "1982"},
        {"sex": "other"},
        {"theme": "sepia"},
        {"weight_unit": "stone"},
        {"temperature_unit": "k"},
        {"datetime_format": "us"},
        {"chart_range": "5y"},
        {"overview_order": ["body", "body"]},
        {"overview_order": ["body", "sleep"]},
        {"overview_order": ["body", "heart", "nerve", "activity", "body"]},
        {"notify_tiers": ["device", "device"]},
        {"notify_tiers": ["urgent"]},
        {"show_unsupported": "yes"},
        {"day_assignment": "fixed_tz"},  # no zone
        {"day_assignment": "fixed_tz", "day_tz": "Mars/Olympus"},
        {"day_assignment": "fixed_tz", "day_tz": "x" * 65},
        {"day_assignment": "reading_tz", "day_tz": "Europe/Stockholm"},
        {"day_assignment": "yearly"},
    ],
)
async def test_an_invalid_profile_is_refused_and_nothing_is_stored(
    env: Env, changes: dict[str, Any]
) -> None:
    browser, _ = await user(env, "a@example.com")
    response = await browser.request("PUT", PROFILE, body(**changes))
    assert response.status_code in {400, 422}, response.text
    assert rows() == []


async def test_every_field_is_required(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    partial = body()
    del partial["theme"]
    assert (await browser.request("PUT", PROFILE, partial)).status_code == 422


async def test_a_fixed_zone_is_stored_with_its_name(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    response = await browser.request(
        "PUT", PROFILE, body(day_assignment="fixed_tz", day_tz="Europe/Stockholm")
    )
    assert response.status_code == 200, response.text
    assert (await browser.get(PROFILE)).json()["day_tz"] == "Europe/Stockholm"


async def test_the_profile_needs_a_signed_in_user_and_the_csrf_token(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    assert (await Browser(env).get(PROFILE)).status_code == 401
    assert (await Browser(env).request("PUT", PROFILE, body())).status_code == 401
    assert (await browser.request("PUT", PROFILE, body(), csrf="wrong")).status_code in {
        400,
        403,
    }
    assert rows() == []


async def test_the_experimental_results_need_the_switch(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    refused = await browser.request("PUT", PROFILE, body(show_experimental=True))
    assert (refused.status_code, refused.json()["detail"]["reason"]) == (
        400,
        "experimental_not_available",
    )
    assert rows() == []


async def test_the_experimental_results_can_be_chosen_when_the_switch_is_on(
    owner_env: Env,
) -> None:
    browser, _ = await user(owner_env, "a@example.com")
    assert (await browser.get(PROFILE)).json()["experimental_available"] is True
    response = await browser.request("PUT", PROFILE, body(show_experimental=True))
    assert response.status_code == 200, response.text
    assert (await browser.get(PROFILE)).json()["show_experimental"] is True


async def test_changing_sex_or_the_day_assignment_queues_the_recompute(env: Env) -> None:
    browser, owner = await user(env, "a@example.com")
    connected_with_streams(owner)
    await browser.request("PUT", PROFILE, body(theme="dark", birth_year=1980))
    assert queued_kinds() == []  # display and birth year never recompute
    await browser.request("PUT", PROFILE, body(theme="dark", birth_year=1980, sex="male"))
    assert queued_kinds() == ["derive_daily"]
    await browser.request("PUT", PROFILE, body(theme="dark", birth_year=1980, sex="male"))
    assert queued_kinds() == ["derive_daily"]  # unchanged: no second pass


async def test_a_zone_change_queues_the_recompute_and_no_connection_means_none(env: Env) -> None:
    quiet, _ = await user(env, "b@example.com")
    await quiet.request("PUT", PROFILE, body(sex="female"))
    assert queued_kinds() == []  # nothing to recompute without a connection
    browser, owner = await user(env, "a@example.com")
    connected_with_streams(owner)
    await browser.request(
        "PUT", PROFILE, body(day_assignment="fixed_tz", day_tz="Europe/Stockholm")
    )
    assert queued_kinds() == ["derive_daily"]


# --- the pure model (no database) ---------------------------------------------------------------


def test_the_defaults_are_a_valid_profile() -> None:
    assert ProfileIn.model_validate(DEFAULTS).day_assignment == "reading_tz"


def test_only_sex_and_day_assignment_move_the_analysis() -> None:
    old = ProfileIn.model_validate(DEFAULTS)
    assert not moves_the_analysis(
        old, ProfileIn.model_validate(body(theme="dark", birth_year=1980))
    )
    assert moves_the_analysis(old, ProfileIn.model_validate(body(sex="male")))
    fixed = body(day_assignment="fixed_tz", day_tz="Europe/Stockholm")
    assert moves_the_analysis(old, ProfileIn.model_validate(fixed))
    assert not moves_the_analysis(
        ProfileIn.model_validate(fixed), ProfileIn.model_validate({**fixed, "theme": "dark"})
    )
    other = body(day_assignment="fixed_tz", day_tz="America/New_York")
    assert moves_the_analysis(ProfileIn.model_validate(fixed), ProfileIn.model_validate(other))
