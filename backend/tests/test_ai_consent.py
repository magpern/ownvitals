"""AI consent and withdrawal (ADR 0014 sections 1 and 3, PLAN section 7.4; #206) on a real
database, through the API and the consent module, as the app role under forced RLS.

Synthetic accounts, jobs and messages only; no model call and no real data.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from ctx import make_context
from flows import Browser, reason, signed_in
from jobtools import admin_url, engine_for, new_job
from ownvitals.ai import consent, consent_text
from ownvitals.ai.consent import AI_JOB_KINDS, AiNotEnabled
from ownvitals.db import user_session

SYNTHETIC = True
TEXT_EN = consent_text.TEXTS["en"]
SHA_EN = consent_text.fingerprint(TEXT_EN).hex()


def _sql(sql: str, **params: Any) -> Any:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return conn.execute(text(sql), params)


def _scalar(sql: str, **params: Any) -> Any:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return conn.execute(text(sql), params).scalar_one_or_none()


def _job(owner: UUID, kind: str, state: str = "queued") -> UUID:
    """A synthetic job of ``owner``; a running one is held by a synthetic worker."""
    now = datetime.now(UTC)
    held: dict[str, Any] = {}
    if state == "running":
        held = {
            "state": "running",
            "attempt": 1,
            "lease_version": 1,
            "leased_by": "synthetic-worker",
            "lease_expires_at": now + timedelta(minutes=10),
            "heartbeat_at": now,
            "started_at": now,
        }
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return new_job(conn, owner, kind=kind, **held)


def _history(owner: UUID, messages: int = 2) -> UUID:
    conversation = _scalar(
        "INSERT INTO ai_conversations (owner_id) VALUES (:o) RETURNING id", o=owner
    )
    for n in range(messages):
        _sql(
            "INSERT INTO ai_messages (owner_id, conversation_id, role, content) "
            "VALUES (:o, :c, :r, :t)",
            o=owner,
            c=conversation,
            r="user" if n % 2 == 0 else "assistant",
            t=f"synthetic message {n}",
        )
    return conversation


def _count(table: str, owner: UUID) -> int:
    assert table in {"ai_messages", "ai_conversations", "ai_consents", "ai_spend"}
    return int(_scalar(f"SELECT count(*) FROM {table} WHERE owner_id = :o", o=owner))  # noqa: S608


@pytest.fixture(name="off")
async def off_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """The app with ``ai_insights`` off (the default)."""
    _sql("DELETE FROM app_settings WHERE flag = 'ai_insights'")
    async with running_env(app_database_url) as running:
        yield running


@pytest.fixture(name="on")
async def on_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """The app with the environment's ``ai_insights`` on and no stored flag."""
    _sql("DELETE FROM app_settings WHERE flag = 'ai_insights'")
    async with running_env(app_database_url, ai_insights=True) as running:
        yield running


async def _user(env: Env, email: str) -> tuple[Browser, UUID]:
    browser = await signed_in(env, email)
    found = await env.scalar("SELECT id FROM users WHERE email_normalized = :e", e=email)
    assert isinstance(found, UUID)
    return browser, found


def _body(**changes: Any) -> dict[str, Any]:
    return {"version": consent_text.CONSENT_VERSION, "locale": "en", "sha256": SHA_EN} | {
        "agree": True,
        **changes,
    }


async def _give(env: Env, email: str) -> tuple[Browser, UUID]:
    browser, owner = await _user(env, email)
    response = await browser.post("/api/ai/consent", _body())
    assert response.status_code == 200, response.text
    return browser, owner


# --- the text and the state ----------------------------------------------------------------------


async def test_the_state_and_the_text_with_its_fingerprint(off: Env) -> None:
    browser, _ = await _user(off, "synthetic-a@example.com")
    body = (await browser.get("/api/ai/consent")).json()
    assert body["enabled"] is False
    assert body["consent"] == {"state": "none", "version": None, "given_at": None}
    assert body["consent_text"] == {
        "version": consent_text.CONSENT_VERSION,
        "locale": "en",
        "text": TEXT_EN,
        "sha256": SHA_EN,
    }
    sv = (await browser.get("/api/ai/consent?locale=sv")).json()["consent_text"]
    assert (sv["locale"], sv["text"]) == ("sv", consent_text.TEXTS["sv"])
    other = (await browser.get("/api/ai/consent?locale=de")).json()["consent_text"]
    assert other["locale"] == "en"  # a language without a text falls back to English
    assert (await browser.get("/api/ai/consent?locale=EN")).status_code == 422


async def test_anonymous_callers_get_nothing(off: Env) -> None:
    anonymous = Browser(off)
    assert (await anonymous.get("/api/ai/consent")).status_code == 401
    assert (await anonymous.request("DELETE", "/api/ai/consent")).status_code in (401, 403)


# --- giving consent ------------------------------------------------------------------------------


@pytest.mark.parametrize("changes", [{}, {"sha256": "0" * 64}, {"version": 2}])
async def test_giving_consent_needs_the_flag(off: Env, changes: dict[str, Any]) -> None:
    browser, owner = await _user(off, "synthetic-b@example.com")
    response = await browser.post("/api/ai/consent", _body(**changes))
    # With the flag off the answer is "not enabled" whatever the text.
    assert (response.status_code, reason(response)) == (409, "ai_not_enabled")
    assert _count("ai_consents", owner) == 0


async def test_giving_consent_needs_a_step_up(on: Env) -> None:
    browser, owner = await _user(on, "synthetic-c@example.com")
    await on.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    response = await browser.post("/api/ai/consent", _body())
    assert (response.status_code, reason(response)) == (403, "step_up_required")
    assert _count("ai_consents", owner) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "0" * 64},
        {"version": consent_text.CONSENT_VERSION + 1},
        {"locale": "sv"},  # the English fingerprint is not the Swedish text's
        {"locale": "de"},
    ],
)
async def test_only_the_exact_current_text_can_be_agreed_to(
    on: Env, changes: dict[str, Any]
) -> None:
    browser, owner = await _user(on, "synthetic-d@example.com")
    response = await browser.post("/api/ai/consent", _body(**changes))
    assert (response.status_code, reason(response)) == (409, "ai_consent_outdated")
    assert _count("ai_consents", owner) == 0


@pytest.mark.parametrize(
    "changes", [{"agree": False}, {"agree": None}, {"extra": 1}, {"sha256": "XYZ"}]
)
async def test_the_box_must_be_ticked_and_the_body_closed(on: Env, changes: dict[str, Any]) -> None:
    browser, owner = await _user(on, "synthetic-e@example.com")
    assert (await browser.post("/api/ai/consent", _body(**changes))).status_code == 422
    assert _count("ai_consents", owner) == 0


async def test_consent_is_recorded_with_the_texts_fingerprint_once(on: Env) -> None:
    browser, owner = await _give(on, "synthetic-f@example.com")
    again = await browser.post("/api/ai/consent", _body())
    assert again.status_code == 200
    assert again.json()["consent"]["state"] == "given"
    row = _sql(
        "SELECT consent_version, locale, text_sha256, withdrawn_at FROM ai_consents "
        "WHERE owner_id = :o",
        o=owner,
    ).one()
    assert (row.consent_version, row.locale, bytes(row.text_sha256).hex()) == (1, "en", SHA_EN)
    assert row.withdrawn_at is None
    ctx = make_context(owner)
    await consent.check_enabled(on.factory, ctx, on.settings)  # flag and consent: enabled


async def test_an_older_text_is_outdated_and_needs_consent_again(on: Env) -> None:
    browser, owner = await _user(on, "synthetic-g@example.com")
    _sql(
        "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
        "VALUES (:o, 1, 'en', :h)",
        o=owner,
        h=b"\x01" * 32,  # the fingerprint of a text that is no longer current
    )
    state = (await browser.get("/api/ai/consent")).json()["consent"]
    assert state["state"] == "outdated"
    with pytest.raises(AiNotEnabled) as refused:
        await consent.check_enabled(on.factory, make_context(owner), on.settings)
    assert refused.value.reason == "consent_outdated"
    assert (await browser.post("/api/ai/consent", _body())).status_code == 200
    assert _count("ai_consents", owner) == 2  # the old one closed, the new one live
    await consent.check_enabled(on.factory, make_context(owner), on.settings)


async def test_without_the_flag_or_consent_ai_is_not_enabled(off: Env, on: Env) -> None:
    owner = await on.user("synthetic-h@example.com")
    with pytest.raises(AiNotEnabled) as none:
        await consent.check_enabled(on.factory, make_context(owner), on.settings)
    assert none.value.reason == "no_consent"
    _sql(
        "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
        "VALUES (:o, 1, 'en', :h)",
        o=owner,
        h=bytes.fromhex(SHA_EN),
    )
    with pytest.raises(AiNotEnabled) as flag:
        await consent.check_enabled(off.factory, make_context(owner), off.settings)
    assert flag.value.reason == "feature_off"
    _sql("INSERT INTO app_settings (flag, value) VALUES ('ai_insights', CAST('\"off\"' AS jsonb))")
    with pytest.raises(AiNotEnabled) as stored:  # a stored "off" wins over the environment
        await consent.check_enabled(on.factory, make_context(owner), on.settings)
    assert stored.value.reason == "feature_off"


# --- withdrawal ----------------------------------------------------------------------------------


async def test_withdrawal_deletes_history_cancels_jobs_and_keeps_the_ledger(on: Env) -> None:
    browser, owner = await _give(on, "synthetic-i@example.com")
    other_browser, other = await _give(on, "synthetic-j@example.com")
    _history(owner, 3)
    _history(owner, 1)
    _history(other, 2)
    _sql("INSERT INTO ai_spend (owner_id, month, spent) VALUES (:o, '2026-11-01', 7)", o=owner)
    queued = _job(owner, "ai_chat_turn")
    running = _job(owner, "ai_digest", "running")
    unrelated = _job(owner, "withings_sync")
    others = _job(other, "ai_chat_turn")

    response = await browser.request("DELETE", "/api/ai/consent")
    assert response.status_code == 200, response.text
    assert response.json() == {"withdrawn": True, "cancelled_jobs": 2, "deleted_messages": 4}
    # The running job's cancel is already requested: a second withdrawal counts nothing.
    again = await browser.request("DELETE", "/api/ai/consent")
    assert again.json() == {"withdrawn": False, "cancelled_jobs": 0, "deleted_messages": 0}

    assert (_count("ai_messages", owner), _count("ai_conversations", owner)) == (0, 0)
    assert _count("ai_spend", owner) == 1  # the spend ledger stays (no content)
    assert _scalar("SELECT withdrawn_at IS NOT NULL FROM ai_consents WHERE owner_id = :o", o=owner)
    states = {
        r.id: (r.state, r.cancel_requested_at is not None)
        for r in _sql("SELECT id, state, cancel_requested_at FROM jobs").all()
    }
    assert states[queued] == ("cancelled", True)  # a queued job ends at once
    assert states[running] == ("running", True)  # a running one stops at its next checkpoint
    assert states[unrelated] == ("queued", False)
    assert states[others] == ("queued", False)
    # The other user's consent and history are untouched.
    assert (_count("ai_messages", other), _count("ai_conversations", other)) == (2, 1)
    assert (await other_browser.get("/api/ai/consent")).json()["consent"]["state"] == "given"
    assert (await browser.get("/api/ai/consent")).json()["consent"]["state"] == "none"
    # A job of the user that checks consent in its own transaction refuses from now on.
    with pytest.raises(AiNotEnabled) as refused:
        async with user_session(on.factory, make_context(owner)) as db:
            await consent.require_consent(db, owner)
    assert refused.value.reason == "no_consent"


async def test_withdrawal_needs_no_step_up_and_works_while_the_flag_is_off(
    on: Env, off: Env
) -> None:
    browser, owner = await _give(on, "synthetic-k@example.com")
    _history(owner)
    queued = _job(owner, "ai_chat_turn")
    signed_off = Browser(off)
    signed_off.__dict__.update(browser.__dict__)  # the same session cookie on the "off" app
    signed_off.env = off
    await off.run("UPDATE sessions SET stepup_at = NULL, reauth_at = now() - interval '1 day'")
    response = await signed_off.request("DELETE", "/api/ai/consent")
    assert response.status_code == 200, response.text
    assert _count("ai_messages", owner) == 0
    assert _scalar("SELECT state FROM jobs WHERE id = :i", i=queued) == "cancelled"
    again = await signed_off.request("DELETE", "/api/ai/consent")  # idempotent, counted once
    assert again.json() == {"withdrawn": False, "cancelled_jobs": 0, "deleted_messages": 0}


def test_the_ai_job_kinds_are_the_adr_kinds() -> None:
    assert AI_JOB_KINDS == ("ai_chat_turn", "ai_digest")


# --- the race between a stored reply and a withdrawal (ADR 0014 section 3) -----------------------


async def test_a_reply_holding_the_consent_row_is_deleted_by_the_withdrawal_after_it(
    on: Env,
) -> None:
    _, owner = await _give(on, "synthetic-l@example.com")
    conversation = _history(owner, 0)
    ctx = make_context(owner)
    checked, release = asyncio.Event(), asyncio.Event()

    async def reply() -> None:
        async with user_session(on.factory, ctx) as db:
            await consent.require_consent(db, owner)  # FOR SHARE, held until the commit
            checked.set()
            await release.wait()
            await db.execute(
                text(
                    "INSERT INTO ai_messages (owner_id, conversation_id, role, content) "
                    "VALUES (:o, :c, 'assistant', 'synthetic reply')"
                ),
                {"o": owner, "c": conversation},
            )

    replying = asyncio.create_task(reply())
    await checked.wait()
    withdrawing = asyncio.create_task(consent.withdraw(on.factory, ctx))
    await asyncio.sleep(0.3)
    assert not withdrawing.done()  # it waits on the consent row's lock
    release.set()
    await replying
    done = await withdrawing
    assert done.closed
    assert done.deleted["ai_messages"] == 1  # the reply committed first, and is deleted
    assert _count("ai_messages", owner) == 0


async def test_a_reply_after_the_withdrawal_sees_no_consent(on: Env) -> None:
    _, owner = await _give(on, "synthetic-m@example.com")
    ctx = make_context(owner)
    await consent.withdraw(on.factory, ctx)
    with pytest.raises(AiNotEnabled):
        async with user_session(on.factory, ctx) as db:
            await consent.require_consent(db, owner)
