"""``GET /api/notifications`` (#195): the account menu's in-app list, in the web-request context
(signed-in synthetic users over the app role, forced RLS). Synthetic data only.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

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
T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


@pytest.fixture(name="env")
async def env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url) as running:
        yield running


async def _user(env: Env, email: str) -> tuple[Browser, UUID]:
    browser = await signed_in(env, email)
    found = await env.scalar("SELECT id FROM users WHERE email_normalized = :e", e=email)
    assert isinstance(found, UUID)
    return browser, found


def sent(owner: UUID, rule: str, at: datetime, groups: tuple[int, ...] = (), **kw: Any) -> UUID:
    finding = plant_finding(owner, rule, groups, **kw)
    run(
        "UPDATE findings SET notify_state = 'sent', notified_at = :t WHERE id = :id",
        id=finding,
        t=at,
    )
    return finding


async def test_anonymous_requests_are_refused(env: Env) -> None:
    response = await Browser(env).get("/api/notifications")
    assert (response.status_code, reason(response)) == (401, "not_authenticated")


async def test_the_list_holds_only_the_users_sent_findings_newest_first(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    _, bob_id = await _user(env, "bob@example.com")
    older = sent(alice_id, "C1", T0, metric_keys=("heart_rate",))
    newer = sent(alice_id, "E1", T0 + timedelta(hours=1), metric_keys=())
    run("UPDATE findings SET seen_at = :t WHERE id = :id", id=older, t=T0)
    plant_finding(alice_id, "B1", metric_keys=("weight",))  # not notified
    sent(bob_id, "C1", T0, metric_keys=("heart_rate",))
    response = await alice.get("/api/notifications")
    assert response.status_code == 200
    body = response.json()
    assert [item["finding_id"] for item in body["notifications"]] == [str(newer), str(older)]
    assert [item["seen"] for item in body["notifications"]] == [False, True]
    assert body["unseen"] == 1
    first = body["notifications"][0]
    assert set(first) == {
        "finding_id",
        "rule_id",
        "tier",
        "area",
        "severity",
        "notified_at",
        "seen",
        "not_evaluated",
    }
    assert (first["rule_id"], first["tier"], first["not_evaluated"]) == ("E1", "device", False)
    limited = await alice.get("/api/notifications?limit=1")
    assert [item["finding_id"] for item in limited.json()["notifications"]] == [str(newer)]
    assert (await alice.get("/api/notifications?limit=51")).status_code == 422


async def test_a_hidden_or_experimental_finding_is_left_out_and_not_evaluated_is_marked(
    env: Env,
) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    connection = plant_user(alice_id)
    plant_group(alice_id, connection, 91, minutes=0)
    plant_recording(alice_id, connection, 1, minutes=0, result="fail")
    sent(alice_id, "E6", T0, (91,))  # hidden now: a later failure
    sent(alice_id, "E4", T0, (404,))  # experimental, without the switch
    shown = sent(alice_id, "E6", T0, (404,))  # no linked recording: not evaluated
    body = (await alice.get("/api/notifications")).json()
    assert [item["finding_id"] for item in body["notifications"]] == [str(shown)]
    assert body["notifications"][0]["not_evaluated"] is True
