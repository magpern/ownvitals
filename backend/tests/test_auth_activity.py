"""The sign-out events and ``GET /api/auth/activity`` (#251; ``auth/activity_routes.py``).

Each way out is recorded once with the request's coarse origin; the list shows a user only their
own rows (``auth_events`` has no RLS, so the route's filter is the wall: guard 7 here), only the
allowlisted kinds and fields, paged and bounded. Synthetic accounts, addresses and agent strings
only.
"""

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import insert, text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    session_cookie,
    unsafe_headers,
)
from flows import signed_in
from ownvitals.auth import routes
from ownvitals.auth.activity_model import PAGE_DEFAULT, PAGE_MAX
from ownvitals.auth.sessions import IssuedSession
from ownvitals.auth.tables import auth_events

# A synthetic agent string: Firefox on Windows. Its distinctive tail must never be echoed.
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
FIELDS = {"kind", "at", "method", "browser", "os", "place"}


async def _csrf(env: Env, issued: IssuedSession) -> str:
    response = await env.client.get("/api/auth/session", headers=session_cookie(issued.token))
    return response.json()["csrf_token"]


async def _unsafe(env: Env, issued: IssuedSession, agent: str = FIREFOX) -> dict[str, str]:
    return {**unsafe_headers(issued.token, await _csrf(env, issued)), "User-Agent": agent}


async def _activity(env: Env, issued: IssuedSession, **params: Any) -> httpx.Response:
    return await env.client.get(
        "/api/auth/activity", params=params, headers=session_cookie(issued.token)
    )


async def _events(env: Env, kind: str) -> list[Any]:
    async with env.db() as db:
        rows = await db.execute(
            text("SELECT user_id, browser, os, place, detail FROM auth_events WHERE kind = :k"),
            {"k": kind},
        )
        return list(rows.all())


async def _plant(env: Env, user_id: UUID | None, kind: str, n: int = 1, **values: Any) -> None:
    async with env.db() as db:
        for _ in range(n):
            await db.execute(insert(auth_events).values(kind=kind, user_id=user_id, **values))


# --- each way out is recorded once --------------------------------------------------------------


async def test_logout_records_one_sign_out_with_the_origin(env: Env) -> None:
    user_id = await env.user()
    issued = await env.login(user_id)
    headers = await _unsafe(env, issued)
    response = await env.client.post("/api/auth/logout", headers=headers)
    assert response.status_code == 204
    rows = await _events(env, "sign_out")
    assert [(r.user_id, r.browser, r.os, r.place, r.detail) for r in rows] == [
        (user_id, "Firefox", "Windows", None, None)
    ]
    # A second sign-out with the dead cookie is anonymous: nothing more is recorded.
    await env.client.post("/api/auth/logout", headers=headers)
    assert len(await _events(env, "sign_out")) == 1


async def test_ending_this_session_from_the_list_is_a_sign_out(env: Env) -> None:
    user_id = await env.user()
    mine = await env.full_login(user_id)
    response = await env.client.delete(
        f"/api/auth/sessions/{mine.id}", headers=await _unsafe(env, mine)
    )
    assert response.status_code == 204
    assert len(await _events(env, "sign_out")) == 1
    assert await _events(env, "session_signed_out") == []


async def test_ending_another_session_is_recorded_once_and_seen_by_the_rest(env: Env) -> None:
    user_id = await env.user()
    mine = await env.full_login(user_id)
    spare = await env.login(user_id)
    third = await env.full_login(user_id)
    headers = await _unsafe(env, mine, SAFARI_IPHONE)
    response = await env.client.delete(f"/api/auth/sessions/{spare.id}", headers=headers)
    assert response.status_code == 204
    gone = await env.client.delete(f"/api/auth/sessions/{spare.id}", headers=headers)
    assert gone.status_code == 404  # already ended: no second event
    rows = await _events(env, "session_signed_out")
    assert [(r.user_id, r.browser, r.os, r.detail) for r in rows] == [
        (user_id, "Safari", "iOS", None)
    ]
    assert await _events(env, "sign_out") == []
    # Every remaining session sees it, the one that did it and the other one.
    for issued in (mine, third):
        events = (await _activity(env, issued)).json()["events"]
        assert [e["kind"] for e in events] == ["session_signed_out"]
        assert events[0]["browser"] == "Safari"
        assert events[0]["os"] == "iOS"


async def test_another_users_session_is_a_404_and_records_nothing(env: Env) -> None:
    mine = await env.full_login(await env.user())
    theirs = await env.login(await env.user("other@example.com"))
    response = await env.client.delete(
        f"/api/auth/sessions/{theirs.id}", headers=await _unsafe(env, mine)
    )
    assert response.status_code == 404
    assert await env.scalar("SELECT count(*) FROM auth_events") == 0


async def test_logout_everywhere_is_recorded_once_with_the_count(env: Env) -> None:
    user_id = await env.user()
    mine = [await env.full_login(user_id, stepup=True) for _ in range(3)]
    response = await env.client.post("/api/auth/logout-all", headers=await _unsafe(env, mine[0]))
    assert response.status_code == 204
    rows = await _events(env, "logout_everywhere")
    assert [(r.user_id, r.browser, r.os, r.detail) for r in rows] == [
        (user_id, "Firefox", "Windows", {"sessions": 3})
    ]
    assert await _events(env, "sign_out") == []


async def test_a_failed_event_write_never_keeps_the_session(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The event write fails (a check violation inside its savepoint): the sign-out still
    commits, and the log line names the class and SQLSTATE only."""

    async def broken(db: Any, kind: str, **_: Any) -> None:
        await db.execute(insert(auth_events).values(kind=kind, place=""))  # violates the check

    monkeypatch.setattr(routes, "record_event", broken)
    issued = await env.login(await env.user())
    response = await env.client.post("/api/auth/logout", headers=await _unsafe(env, issued))
    assert response.status_code == 204
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await env.scalar("SELECT count(*) FROM auth_events") == 0
    lines = [r.getMessage() for r in caplog.records if r.name == routes.logger.name]
    assert lines == ["sign-out event write failed: IntegrityError (SQLSTATE 23514)"]


async def test_a_sign_in_through_the_api_carries_its_method_and_origin(env: Env) -> None:
    browser = await signed_in(env)  # password, then the TOTP code
    page = (await browser.get("/api/auth/activity")).json()
    assert [(e["kind"], e["method"]) for e in page["events"]] == [("sign_in", "password")]
    assert page["events"][0]["browser"] == "other"  # httpx's own agent: no known family
    assert page["events"][0]["place"] == "Place unknown"  # geo_source is none by default
    assert page["next_before"] is None


# --- what the list shows ------------------------------------------------------------------------


async def test_the_response_holds_only_the_allowlisted_fields(env: Env) -> None:
    """A payload scan: rows planted with every value the list must not show."""
    user_id = await env.user("planted-owner@example.com")
    issued = await env.full_login(user_id)
    session_id = str(issued.id)
    planted = {
        "ip": "203.0.113.77",
        "detail": {
            "session": session_id,
            "email": "planted-owner@example.com",
            "token": "planted-token-" + "T" * 24,
            "passkey": "11111111-2222-3333-4444-555555555555",
            "user_agent": FIREFOX,
            "provider": "google",
            "why": "planted-reason",
        },
        "browser": "Firefox",
        "os": "Windows",
        "place": "Malmö, SE",
    }
    for kind in ("sign_in", "qr_login_completed", "reauth_failed", "session_signed_out"):
        await _plant(env, user_id, kind, **planted)
    await _plant(env, user_id, "passkey_signin_failed", detail={"purpose": "reauth"})
    response = await _activity(env, issued)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"events", "next_before"}
    for event in body["events"]:
        assert set(event) == FIELDS
    raw = response.text
    for value in (
        "203.0.113.77",
        session_id,
        str(user_id),
        "planted-owner",
        "T" * 24,
        "11111111-2222",
        "Gecko/20100101",
        "planted-reason",
        "pytest",  # the session's stored agent string
        issued.token,
    ):
        assert value not in raw
    kinds = [(e["kind"], e["method"]) for e in body["events"]]
    assert sorted(kinds, key=str) == sorted(
        [
            ("sign_in", "google"),
            ("sign_in", "qr"),
            ("reauth_failed", "google"),
            ("session_signed_out", None),
            ("reauth_failed", "passkey"),
        ],
        key=str,
    )
    assert {e["place"] for e in body["events"]} == {"Malmö, SE", "Place unknown"}


async def test_hidden_kinds_and_unknown_families_stay_out(env: Env) -> None:
    user_id = await env.user()
    issued = await env.full_login(user_id)
    for kind in ("connections_wiped", "qr_login_started", "step_up", "admin_granted"):
        await _plant(env, user_id, kind)
    await _plant(env, user_id, "sign_out", browser="NotABrowser<script>", os="TempleOS")
    await _plant(env, user_id, "mfa_reset", browser="Firefox", os="Windows", place="Oslo, NO")
    events = (await _activity(env, issued)).json()["events"]
    shown = {e["kind"]: e for e in events}
    assert set(shown) == {"sign_out", "mfa_reset"}
    assert (shown["sign_out"]["browser"], shown["sign_out"]["os"]) == ("other", "other")
    # The operator's reset is not the user's doing: no origin is shown for it.
    reset = shown["mfa_reset"]
    assert (reset["browser"], reset["os"], reset["place"]) == (None, None, "Place unknown")


# --- guard 7: only the caller's own rows ---------------------------------------------------------


async def test_each_user_reads_only_their_own_events(env: Env) -> None:
    alice_id = await env.user("alice@example.com")
    bob_id = await env.user("bob@example.com")
    alice = await env.full_login(alice_id)
    bob = await env.full_login(bob_id)
    await _plant(env, alice_id, "password_changed", n=3)
    await _plant(env, bob_id, "totp_removed", n=2)
    await _plant(env, None, "sign_in_failed", n=2)  # an unknown address: nobody's
    mine = (await _activity(env, alice, user_id=str(bob_id))).json()  # an extra param is ignored
    assert [e["kind"] for e in mine["events"]] == ["password_changed"] * 3
    theirs = (await _activity(env, bob)).json()
    assert [e["kind"] for e in theirs["events"]] == ["totp_removed"] * 2
    # A cursor made from Alice's rows only bounds Bob's own query.
    first = (await _activity(env, alice, limit=1)).json()
    assert first["next_before"]
    with_hers = (await _activity(env, bob, before=first["next_before"])).json()
    assert {e["kind"] for e in with_hers["events"]} <= {"totp_removed"}


async def test_the_list_needs_a_full_session(env: Env) -> None:
    anonymous = await env.client.get("/api/auth/activity")
    assert anonymous.status_code == 401
    half = await env.login(await env.user())  # the second factor still owed
    response = await _activity(env, half)
    assert response.status_code == 403
    assert response.json()["detail"] == {"reason": "two_factor_required"}


# --- paging -------------------------------------------------------------------------------------


async def test_pages_are_bounded_and_never_skip_or_repeat_a_row(env: Env) -> None:
    user_id = await env.user()
    issued = await env.full_login(user_id)
    # 45 rows in one statement's time: ties in ``at`` are ordered by id.
    async with env.db() as db:
        await db.execute(
            text(
                "INSERT INTO auth_events (user_id, kind, at) "
                "SELECT :u, 'password_changed', now() FROM generate_series(1, 45)"
            ),
            {"u": user_id},
        )
    seen: list[int] = []
    cursor = None
    while True:
        params: dict[str, Any] = {} if cursor is None else {"before": cursor}
        page = (await _activity(env, issued, **params)).json()
        seen.append(len(page["events"]))
        cursor = page["next_before"]
        if cursor is None:
            break
    assert seen == [PAGE_DEFAULT, PAGE_DEFAULT, 5]
    ids = await env.scalar(
        "SELECT count(DISTINCT id) FROM auth_events WHERE user_id = :u", u=user_id
    )
    assert ids == sum(seen) == 45
    # Every page's cursor is strictly older: walk with limit=PAGE_MAX and compare the sets.
    full = (await _activity(env, issued, limit=PAGE_MAX)).json()
    assert len(full["events"]) == 45
    assert full["next_before"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": PAGE_MAX + 1},
        {"limit": "many"},
        {"before": "not-a-cursor"},
        {"before": "9" * 19 + "_" + "0" * 32},  # past any datetime
        {"before": "1_" + "0" * 31},
        {"before": "1_" + "0" * 32 + "x" * 40},
    ],
)
async def test_bad_paging_input_is_a_422_that_echoes_nothing(
    env: Env, params: dict[str, Any]
) -> None:
    issued = await env.full_login(await env.user())
    response = await _activity(env, issued, **params)
    assert response.status_code == 422
    for value in params.values():
        if isinstance(value, str) and len(value) > 4:
            assert value not in response.text


async def test_the_cursor_round_trips_exactly(env: Env) -> None:
    user_id = await env.user()
    issued = await env.full_login(user_id)
    await _plant(env, user_id, "totp_enrolled")
    await _plant(env, user_id, "totp_replaced")
    first = (await _activity(env, issued, limit=1)).json()
    second = (await _activity(env, issued, limit=1, before=first["next_before"])).json()
    assert len(first["events"]) == len(second["events"]) == 1
    assert first["events"] != second["events"]
    assert second["next_before"] is None
    assert json.dumps(second).count("totp_") == 1
