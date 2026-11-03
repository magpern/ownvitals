"""``GET /api/findings`` and ``GET /api/findings/{id}`` in the web-request context: signed-in
synthetic users over the app role, forced RLS. Another user's finding, a hidden one and an
experimental one without the switch all answer 404. Synthetic data only.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from flows import Browser, reason, signed_in
from test_views_findings import (
    plant_finding,
    plant_group,
    plant_recording,
    plant_user,
    run,
    store_flag,
)

SYNTHETIC = True


@pytest.fixture(name="env")
async def env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url) as running:
        yield running


@pytest.fixture(name="owner_env")
async def owner_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url, experimental_findings=True) as running:
        yield running


async def _user(env: Env, email: str) -> tuple[Browser, UUID]:
    browser = await signed_in(env, email)
    found = await env.scalar("SELECT id FROM users WHERE email_normalized = :e", e=email)
    assert isinstance(found, UUID)
    return browser, found


def _ids(response: object) -> set[str]:
    return {item["id"] for item in response.json()["findings"]}  # type: ignore[attr-defined]


async def test_anonymous_and_half_signed_in_requests_are_refused(env: Env) -> None:
    anonymous = Browser(env)
    response = await anonymous.get("/api/findings")
    assert (response.status_code, reason(response)) == (401, "not_authenticated")
    half = await signed_in(env, "half@example.com", second_factor=False)
    response = await half.get(f"/api/findings/{uuid4()}")
    assert response.status_code == 403


async def test_a_user_sees_only_their_own_findings(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    bob, bob_id = await _user(env, "bob@example.com")
    mine = plant_finding(alice_id, "C1", metric_keys=("heart_rate",))
    theirs = plant_finding(bob_id, "C1", metric_keys=("heart_rate",))
    listed = await alice.get("/api/findings")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert _ids(listed) == {str(mine)}
    assert _ids(await bob.get("/api/findings")) == {str(theirs)}
    assert (await alice.get(f"/api/findings/{theirs}")).status_code == 404
    assert (await alice.get(f"/api/findings/{uuid4()}")).status_code == 404
    detail = await alice.get(f"/api/findings/{mine}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["finding"]["why_key"] == "finding.C1.why"
    assert body["finding"]["isnt_key"] == "finding.C1.isnt"
    assert body["finding"]["data_quality"] == {}
    assert body["evidence"]["synthetic"] is True
    assert body["readings"] == []
    assert body["estimates"] == []


async def test_filters_and_their_validation(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    live = plant_finding(alice_id, "C1", metric_keys=("heart_rate",))
    history = plant_finding(alice_id, "B2", origin="backfill", metric_keys=("weight",))
    assert _ids(await alice.get("/api/findings")) == {str(live)}  # the live tab by default
    assert _ids(await alice.get("/api/findings?tab=history")) == {str(history)}
    assert _ids(await alice.get("/api/findings?tab=history&area=heart")) == set()
    assert _ids(await alice.get("/api/findings?tier=trend&area=heart")) == {str(live)}
    for bad in ("tab=all", "tier=clinical", "area=lungs", "tier=trend%27"):
        assert (await alice.get(f"/api/findings?{bad}")).status_code == 422
    assert (await alice.get("/api/findings/not-a-uuid")).status_code == 422


async def test_experimental_and_hidden_findings_answer_404(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    connection = plant_user(alice_id)
    e4 = plant_finding(alice_id, "E4")
    plant_group(alice_id, connection, 11, minutes=0)
    plant_recording(alice_id, connection, 1, minutes=0, result="fail")
    e6 = plant_finding(alice_id, "E6", [11])
    e1 = plant_finding(alice_id, "E1", [11], metric_keys=())
    assert _ids(await alice.get("/api/findings")) == {str(e1)}
    assert _ids(await alice.get("/api/findings?tier=experimental")) == set()
    assert (await alice.get(f"/api/findings/{e4}")).status_code == 404
    assert (await alice.get(f"/api/findings/{e6}")).status_code == 404
    shown = await alice.get(f"/api/findings/{e1}")
    assert shown.status_code == 200
    assert shown.json()["finding"]["gate"] is None  # our outcome only behind the switch
    assert shown.json()["estimates"] == []


async def test_the_switch_shows_experimental_findings_marks_and_estimates(
    owner_env: Env,
) -> None:
    alice, alice_id = await _user(owner_env, "alice@example.com")
    connection = plant_user(alice_id)
    plant_group(alice_id, connection, 21, minutes=0)
    recording = plant_recording(alice_id, connection, 1, minutes=0, result="pass")
    e4 = plant_finding(alice_id, "E4", [21])
    unlinked = plant_finding(alice_id, "E6", [99])
    listed = (await alice.get("/api/findings")).json()["findings"]
    by_id = {item["id"]: item for item in listed}
    assert set(by_id) == {str(e4), str(unlinked)}
    assert by_id[str(e4)]["gate"] == {"outcome": "passed", "counted": 1, "failed": 0}
    assert by_id[str(unlinked)]["not_evaluated"] is True
    detail = (await alice.get(f"/api/findings/{e4}")).json()
    (estimate,) = detail["estimates"]
    assert estimate["recording_id"] == str(recording)
    assert (estimate["gate"], estimate["hr_bpm"]) == ("pass", 61.5)
    run("UPDATE ecg_recordings SET estimated = NULL WHERE id = :i", i=recording)
    detail = (await alice.get(f"/api/findings/{e4}")).json()
    assert detail["finding"]["not_evaluated"] is True
    assert detail["estimates"][0]["gate"] is None
    assert detail["estimates"][0]["hr_bpm"] is None
