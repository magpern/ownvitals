"""The weekly digest's API and data lifecycle (issue 209; ADR 0014 sections 3 and 5): ``GET`` and
``PUT /api/ai/digest`` for the signed-in user only (RLS: another user's digests are never shown);
opting in only while AI is enabled for the user, opting out always; withdrawal deletes the stored
digests and the opt-in; "delete imported data" deletes the digests and keeps the opt-in; the
"waiting" mail holds no health content in any language.

Synthetic users, dates and texts; no model call, no network.
"""

from datetime import timedelta
from typing import Any

from aichat import (  # noqa: F401 - fixtures
    ChatEnv,
    _ai_worker,
    chat_fixture,
    chat_secrets_fixture,
    sql,
)
from aidigest import DIGEST_TEXT, WEEK, digest_user, digests, opt_in
from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from ctx import make_context
from flows import reason
from ownvitals.connections.purge import purge_imported
from ownvitals.notify.templates import TABLES, render
from ownvitals.response_codes import AI
from test_response_codes import assert_response_code

SYNTHETIC = True
BASE = "https://ownvitals.example.test"


def _plant(owner: Any, week: Any, status: str, content: str | None) -> None:
    sql(
        "INSERT INTO ai_digests (owner_id, week_start, status, content) VALUES (:o, :w, :s, :c)",
        o=owner,
        w=week,
        s=status,
        c=content,
    )


def _prefs(owner: Any) -> int:
    return int(
        sql("SELECT count(*) FROM ai_digest_prefs WHERE owner_id = :o", o=owner).scalar_one()
    )


async def test_the_view_shows_the_users_own_digests_newest_first(chat: ChatEnv) -> None:
    alice = await digest_user(chat, "synthetic-routes-a@example.com")
    bob = await digest_user(chat, "synthetic-routes-b@example.com")
    _plant(alice.id, WEEK, "ready", DIGEST_TEXT)
    _plant(alice.id, WEEK - timedelta(weeks=1), "skipped_budget", None)
    _plant(bob.id, WEEK, "ready", "synthetic digest of the other user")
    response = await alice.user.browser.get("/api/ai/digest")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["enabled"] is True
    assert [(d["week_start"], d["status"], d["content"]) for d in body["digests"]] == [
        (WEEK.isoformat(), "ready", DIGEST_TEXT),
        ((WEEK - timedelta(weeks=1)).isoformat(), "skipped_budget", None),
    ]
    assert set(body["digests"][0]) == {"week_start", "status", "content", "created_at"}
    assert "other user" not in response.text


async def test_opting_in_needs_ai_enabled_and_opting_out_always_works(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-routes-c@example.com", opted_in=False)
    browser = who.user.browser
    assert (await browser.get("/api/ai/digest")).json()["enabled"] is False  # default off
    on = await browser.request("PUT", "/api/ai/digest", {"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json() == {"enabled": True}
    assert _prefs(who.id) == 1
    again = await browser.request("PUT", "/api/ai/digest", {"enabled": True})
    assert again.status_code == 200  # idempotent
    assert _prefs(who.id) == 1
    off = await browser.request("PUT", "/api/ai/digest", {"enabled": False})
    assert off.json() == {"enabled": False}
    assert _prefs(who.id) == 0
    # The flag off: opting in is refused, opting out still works.
    sql("INSERT INTO app_settings (flag, value) VALUES ('ai_insights', '\"off\"')")
    refused = await browser.request("PUT", "/api/ai/digest", {"enabled": True})
    assert refused.status_code == 409
    assert reason(refused) == "ai_not_enabled"
    assert_response_code("ai", refused.json())
    assert reason(refused) in AI
    assert _prefs(who.id) == 0
    opt_in(who.id)
    assert (await browser.request("PUT", "/api/ai/digest", {"enabled": False})).status_code == 200
    assert _prefs(who.id) == 0
    view = (await browser.get("/api/ai/digest")).json()
    assert view["available"] is False


async def test_opting_in_without_a_consent_is_refused(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-routes-d@example.com", consent=False, opted_in=False)
    refused = await who.user.browser.request("PUT", "/api/ai/digest", {"enabled": True})
    assert refused.status_code == 409
    assert reason(refused) == "ai_not_enabled"
    assert _prefs(who.id) == 0
    sql("INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
        "VALUES (:o, 1, 'en', decode(repeat('00', 32), 'hex'))", o=who.id)  # fmt: skip
    outdated = await who.user.browser.request("PUT", "/api/ai/digest", {"enabled": True})
    assert outdated.status_code == 409  # a consent to another text is not a consent
    assert _prefs(who.id) == 0


async def test_the_body_is_closed(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-routes-e@example.com", opted_in=False)
    for body in ({"enabled": "yes"}, {"enabled": True, "owner_id": str(who.id)}, {}):
        response = await who.user.browser.request("PUT", "/api/ai/digest", body)
        assert response.status_code == 422, body
    assert _prefs(who.id) == 0


async def test_the_routes_need_a_session(chat: ChatEnv) -> None:
    response = await chat.env.client.get("/api/ai/digest")
    assert response.status_code == 401


async def test_withdrawal_deletes_the_digests_and_the_opt_in_and_keeps_the_others(
    chat: ChatEnv,
) -> None:
    alice = await digest_user(chat, "synthetic-routes-f@example.com")
    bob = await digest_user(chat, "synthetic-routes-g@example.com")
    _plant(alice.id, WEEK, "ready", DIGEST_TEXT)
    _plant(bob.id, WEEK, "ready", DIGEST_TEXT)
    response = await alice.user.browser.request("DELETE", "/api/ai/consent")
    assert response.status_code == 200, response.text
    assert digests(alice.id) == []
    assert _prefs(alice.id) == 0
    assert digests(bob.id) == [(WEEK, "ready", DIGEST_TEXT)]
    assert _prefs(bob.id) == 1
    view = (await alice.user.browser.get("/api/ai/digest")).json()
    assert view == {"available": False, "enabled": False, "digests": []}


async def test_delete_imported_data_deletes_the_digests_and_keeps_the_opt_in(
    chat: ChatEnv,
) -> None:
    who = await digest_user(chat, "synthetic-routes-h@example.com")
    _plant(who.id, WEEK, "ready", DIGEST_TEXT)
    await purge_imported(chat.env.factory, make_context(who.id))
    assert digests(who.id) == []
    assert _prefs(who.id) == 1  # a setting, like the consent: deleting data is not opting out


async def test_account_deletion_removes_the_digests_and_the_opt_in(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-routes-i@example.com")
    _plant(who.id, WEEK, "ready", DIGEST_TEXT)
    sql("DELETE FROM users WHERE id = :o", o=who.id)
    assert digests(who.id) == []
    assert _prefs(who.id) == 0


def test_the_waiting_mail_holds_no_health_content_in_any_language() -> None:
    for locale, table in TABLES.items():
        mail = render("ai_digest_ready", "assistant", BASE, locale)
        assert mail.body.count(BASE + "/assistant") == 1
        template = table["ai_digest_ready"]
        words = f"{template.subject}\n{template.body}".lower()
        for forbidden in (
            "weight", "vikt", "pulse", "puls", "heart", "hjärta", "nerve", "nerv", "finding",
            "fynd", "score", "ecg", "ekg", "kg", "bpm", "trend", "synthetic",
        ):  # fmt: skip
            assert forbidden not in words, (locale, forbidden)
        assert not any(c.isdigit() for c in mail.body.replace(BASE, "")), locale
        assert DIGEST_TEXT not in mail.body
