"""Email preferences per kind and the outbox family's two new functions (ADR 0019; #459), against
the migrated schema, the app role and the in-process SMTP fake. Synthetic accounts
(``example.test``) only; no health data.

- the table admits exactly the suppressible kinds (``test_the_kinds_are_exactly_the_suppressible``);
- ``email_unsubscribe`` switches exactly one row off at the token's generation, and refuses a
  replay, a stale generation after a change in the settings, another kind and a gone account
  (``test_unsubscribe_*``); it is the only write the app role has without a user, which RLS
  refuses (``test_without_a_user_the_app_role_reaches_no_preference``);
- ``mail_outbox_email_pref`` answers only for a claimed row of a suppressible kind
  (``test_the_preference_read_*``);
- the settings store changes only the caller's own rows and counts each change
  (``test_the_store_*``);
- web's sender: a switched-off kind is not sent; a suppressible mail carries a verifiable token
  in its footer and the RFC 8058 headers; ``export_ready`` and owner notices never read a
  preference and carry neither (``test_the_sender_*``).
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ctx import make_context
from dbtools import migrated
from jobtools import admin_url
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.mail import Mailer
from ownvitals.notify import email_prefs_store, outbox, unsubscribe
from ownvitals.notify.email_policy import ALWAYS_SENT_KINDS, SUPPRESSIBLE_KINDS
from ownvitals.notify.outbox import USER_KINDS, EmailPref
from ownvitals.notify.sender import OutboxSender, Report
from smtpfake import SmtpFake

BASE = "https://ownvitals.example.test"
KEY = unsubscribe.unsubscribe_key(b"synthetic-app-secret-for-the-email-prefs-tests")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
Factory = async_sessionmaker[AsyncSession]
ROUTES = {
    "findings_waiting": "findings",
    "connection_attention": "connections",
    "ai_digest_ready": "assistant",
    "export_ready": "settings_data",
}


@pytest.fixture(scope="module", name="schema")
def schema_fixture(role_urls: object) -> Iterator[None]:
    with migrated(role_urls.owner):  # type: ignore[attr-defined]
        yield


@pytest.fixture(name="su")
def su_fixture(schema: None) -> Iterator[Connection]:
    engine = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("TRUNCATE mail_outbox, owner_notices, email_prefs"))
            connection.execute(text("UPDATE users SET role = 'user'"))
            yield connection
    finally:
        engine.dispose()


@pytest.fixture(name="factory")
async def factory_fixture(schema: None, app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_async_engine(app_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _account(su: Connection) -> tuple[uuid.UUID, str]:
    user = uuid.uuid4()
    email = f"synthetic-{user.hex[:12]}@example.test"
    su.execute(
        text(
            "INSERT INTO users (id, email, email_normalized, email_verified_at) "
            "VALUES (:id, :e, :e, now())"
        ),
        {"id": user, "e": email},
    )
    return user, email


def _prefs(su: Connection) -> set[tuple[uuid.UUID, str, bool, int]]:
    rows = su.execute(text("SELECT owner_id, kind, enabled, generation FROM email_prefs")).all()
    return {(r.owner_id, str(r.kind), bool(r.enabled), int(r.generation)) for r in rows}


def _mail_for(su: Connection, owner: uuid.UUID, kind: str) -> uuid.UUID:
    return su.execute(
        text("INSERT INTO mail_outbox (owner_id, kind, route) VALUES (:o, :k, :r) RETURNING id"),
        {"o": owner, "k": kind, "r": ROUTES[kind]},
    ).scalar_one()


def _states(su: Connection) -> dict[str, str]:
    rows = su.execute(text("SELECT kind, state FROM mail_outbox")).all()
    return {str(k): str(s) for k, s in rows}


def _mailer(port: int) -> Mailer:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_tls="none",
        smtp_sender="ownvitals@example.test",
        public_base_url=BASE,
    )
    return Mailer(settings)


# --- the table ---------------------------------------------------------------------------------


def test_the_kinds_are_exactly_the_suppressible(su: Connection) -> None:
    check = su.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'email_prefs_kind'"
        )
    ).scalar_one()
    assert sorted(k for k in USER_KINDS if f"'{k}'" in check) == sorted(SUPPRESSIBLE_KINDS)
    assert set(SUPPRESSIBLE_KINDS) | set(ALWAYS_SENT_KINDS) == set(USER_KINDS)
    assert not set(SUPPRESSIBLE_KINDS) & set(ALWAYS_SENT_KINDS)
    alice, _ = _account(su)
    for kind in (*ALWAYS_SENT_KINDS, "connection_needs_reconnect", "password_reset"):
        with pytest.raises(DBAPIError, match="email_prefs_kind"):
            su.execute(
                text("INSERT INTO email_prefs (owner_id, kind) VALUES (:o, :k)"),
                {"o": alice, "k": kind},
            )


# --- the unsubscribe ---------------------------------------------------------------------------


async def test_unsubscribe_switches_exactly_one_row_off(su: Connection, factory: Factory) -> None:
    alice, _ = _account(su)
    bob, _ = _account(su)
    # Bob's own choice and Alice's other kind are rows that must not move.
    su.execute(
        text(
            "INSERT INTO email_prefs (owner_id, kind, enabled, generation) VALUES "
            "(:b, 'findings_waiting', true, 3), (:a, 'ai_digest_ready', true, 2)"
        ),
        {"a": alice, "b": bob},
    )
    before = _prefs(su)
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 0) is True
    after = _prefs(su)
    assert after - before == {(alice, "findings_waiting", False, 1)}
    assert before - after == set()  # nothing else changed (no row of Alice's kind before)
    # Again at an existing row's generation: that row only.
    assert await outbox.unsubscribe(factory, alice, "ai_digest_ready", 2) is True
    final = _prefs(su)
    assert final - after == {(alice, "ai_digest_ready", False, 3)}
    assert after - final == {(alice, "ai_digest_ready", True, 2)}
    assert (bob, "findings_waiting", True, 3) in final


async def test_unsubscribe_refuses_a_replay(su: Connection, factory: Factory) -> None:
    alice, _ = _account(su)
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 0) is True
    state = _prefs(su)
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 0) is False
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 1) is False  # already off
    assert _prefs(su) == state


async def test_unsubscribe_refuses_an_old_link_after_a_change_in_the_settings(
    su: Connection, factory: Factory
) -> None:
    """Off by the link, on again in the settings: the first link (generation 0) and the second
    (generation 1) no longer work; a link of the current generation does."""
    alice, _ = _account(su)
    ctx = make_context(alice)
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 0) is True
    assert (await email_prefs_store.set_enabled(factory, ctx, "findings_waiting", True))[
        "findings_waiting"
    ] is True
    assert _prefs(su) == {(alice, "findings_waiting", True, 2)}
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 0) is False
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 1) is False
    assert _prefs(su) == {(alice, "findings_waiting", True, 2)}
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 2) is True
    assert _prefs(su) == {(alice, "findings_waiting", False, 3)}


async def test_unsubscribe_of_a_gone_account_changes_nothing(
    su: Connection, factory: Factory
) -> None:
    assert await outbox.unsubscribe(factory, uuid.uuid4(), "findings_waiting", 0) is False
    assert _prefs(su) == set()


@pytest.mark.parametrize(
    ("kind", "generation"),
    [("export_ready", 0), ("connection_needs_reconnect", 0), ("findings_waiting", -1)],
)
async def test_unsubscribe_refuses_a_kind_that_cannot_be_switched_off(
    su: Connection, factory: Factory, kind: str, generation: int
) -> None:
    alice, _ = _account(su)
    with pytest.raises(DBAPIError, match="invalid unsubscribe"):
        await outbox.unsubscribe(factory, alice, kind, generation)
    assert _prefs(su) == set()


async def test_without_a_user_the_app_role_reaches_no_preference(
    su: Connection, factory: Factory
) -> None:
    """The web context of the endpoint (the app role, no ``UserContext``): RLS shows it no row and
    refuses its insert and update, so the definer function is its only way to write one."""
    alice, _ = _account(su)
    su.execute(
        text("INSERT INTO email_prefs (owner_id, kind) VALUES (:o, 'findings_waiting')"),
        {"o": alice},
    )
    async with system_session(factory) as db:
        assert (await db.execute(text("SELECT count(*) FROM email_prefs"))).scalar_one() == 0
        updated = await db.execute(text("UPDATE email_prefs SET enabled = false"))
        assert updated.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with system_session(factory) as db:
            await db.execute(
                text("INSERT INTO email_prefs (owner_id, kind) VALUES (:o, 'ai_digest_ready')"),
                {"o": alice},
            )
    assert _prefs(su) == {(alice, "findings_waiting", True, 0)}


# --- the preference read -----------------------------------------------------------------------


async def test_the_preference_read_answers_for_a_claimed_row_only(
    su: Connection, factory: Factory
) -> None:
    alice, _ = _account(su)
    row = _mail_for(su, alice, "findings_waiting")
    assert await outbox.email_pref(factory, row) is None  # pending, not claimed
    await outbox.claim(factory, 10, 60)
    assert await outbox.email_pref(factory, row) == EmailPref(enabled=True, generation=0)
    await outbox.unsubscribe(factory, alice, "findings_waiting", 0)
    assert await outbox.email_pref(factory, row) == EmailPref(enabled=False, generation=1)
    assert await outbox.email_pref(factory, uuid.uuid4()) is None


async def test_the_preference_read_has_nothing_for_a_kind_always_sent(
    su: Connection, factory: Factory
) -> None:
    alice, _ = _account(su)
    row = _mail_for(su, alice, "export_ready")
    await outbox.claim(factory, 10, 60)
    assert await outbox.email_pref(factory, row) is None


# --- the settings store ------------------------------------------------------------------------


async def test_the_store_shows_every_kind_on_by_default_and_changes_only_the_caller(
    su: Connection, factory: Factory
) -> None:
    alice, _ = _account(su)
    bob, _ = _account(su)
    a, b = make_context(alice), make_context(bob)
    assert dict(await email_prefs_store.read(factory, a)) == dict.fromkeys(SUPPRESSIBLE_KINDS, True)
    after = await email_prefs_store.set_enabled(factory, a, "ai_digest_ready", False)
    assert after["ai_digest_ready"] is False
    assert dict(await email_prefs_store.read(factory, b)) == dict.fromkeys(SUPPRESSIBLE_KINDS, True)
    assert _prefs(su) == {(alice, "ai_digest_ready", False, 1)}
    # The same choice again changes nothing (no new generation); a change counts once.
    await email_prefs_store.set_enabled(factory, a, "ai_digest_ready", False)
    assert _prefs(su) == {(alice, "ai_digest_ready", False, 1)}
    await email_prefs_store.set_enabled(factory, a, "ai_digest_ready", True)
    assert _prefs(su) == {(alice, "ai_digest_ready", True, 2)}
    with pytest.raises(ValueError, match="not a suppressible"):
        await email_prefs_store.set_enabled(factory, a, "export_ready", False)


async def test_the_store_saving_on_without_a_row_changes_no_generation(
    su: Connection, factory: Factory
) -> None:
    """No row is "on at generation 0": saving "on" keeps a link of generation 0 working."""
    alice, _ = _account(su)
    await email_prefs_store.set_enabled(factory, make_context(alice), "findings_waiting", True)
    assert _prefs(su) == {(alice, "findings_waiting", True, 0)}
    assert await outbox.unsubscribe(factory, alice, "findings_waiting", 0) is True
    assert _prefs(su) == {(alice, "findings_waiting", False, 1)}


# --- the sender --------------------------------------------------------------------------------


async def test_the_sender_does_not_send_a_switched_off_kind(
    su: Connection, factory: Factory
) -> None:
    alice, _ = _account(su)
    await email_prefs_store.set_enabled(factory, make_context(alice), "findings_waiting", False)
    _mail_for(su, alice, "findings_waiting")
    _mail_for(su, alice, "ai_digest_ready")  # still on
    async with SmtpFake() as smtp:
        report = await OutboxSender(factory, _mailer(smtp.port), BASE, KEY).run_once()
    assert report == Report(sent=1, suppressed=1)
    assert [m.subject for m in smtp.messages] == ["ownvitals: your weekly summary is waiting"]
    assert _states(su) == {"findings_waiting": "failed", "ai_digest_ready": "sent"}


@pytest.mark.parametrize("kind", SUPPRESSIBLE_KINDS)
async def test_the_sender_signs_a_suppressible_mail_with_the_rfc_8058_headers(
    su: Connection, factory: Factory, kind: str
) -> None:
    alice, address = _account(su)
    # A generation other than 0: the token must name the current one.
    su.execute(
        text("INSERT INTO email_prefs (owner_id, kind, generation) VALUES (:o, :k, 4)"),
        {"o": alice, "k": kind},
    )
    _mail_for(su, alice, kind)
    async with SmtpFake() as smtp:
        sender = OutboxSender(factory, _mailer(smtp.port), BASE, KEY, clock=lambda: NOW)
        assert await sender.run_once() == Report(sent=1)
    [mail] = smtp.to(address)
    header = str(mail.message["List-Unsubscribe"])
    assert header.startswith(f"<{BASE}/api/unsubscribe?t=")
    assert header.endswith(">")
    assert str(mail.message["List-Unsubscribe-Post"]) == "List-Unsubscribe=One-Click"
    link = header[1:-1]
    assert mail.text.count(link) == 1  # the footer's link is the header's
    token = link.rpartition("=")[2]
    claim = unsubscribe.verify(KEY, token, NOW)
    assert claim is not None
    assert (claim.owner_id, claim.kind, claim.generation) == (alice, kind, 4)
    assert unsubscribe.verify(KEY, token, NOW + unsubscribe.LIFETIME) is None


async def test_the_sender_sends_an_export_ready_mail_without_reading_a_preference(
    su: Connection, factory: Factory
) -> None:
    """``export_ready`` is never suppressible: no preference is read and no link is added, even
    if a lookup would say "off"."""
    alice, address = _account(su)
    _mail_for(su, alice, "export_ready")

    async def never(_factory: Factory, _row: uuid.UUID) -> EmailPref | None:
        raise AssertionError("an always-sent kind read a preference")

    async with SmtpFake() as smtp:
        sender = OutboxSender(factory, _mailer(smtp.port), BASE, KEY, pref_lookup=never)
        assert await sender.run_once() == Report(sent=1)
    [mail] = smtp.to(address)
    assert mail.message["List-Unsubscribe"] is None
    assert mail.message["List-Unsubscribe-Post"] is None
    assert "/api/unsubscribe" not in mail.text


async def test_the_sender_sends_an_owner_notice_without_a_link(
    su: Connection, factory: Factory
) -> None:
    _, admin = _account(su)
    su.execute(text("UPDATE users SET role = 'admin' WHERE email = :e"), {"e": admin})
    su.execute(text("SELECT owner_notice_queue('connection_needs_reconnect')"))

    async def never(_factory: Factory, _row: uuid.UUID) -> EmailPref | None:
        raise AssertionError("an owner notice read a preference")

    async with SmtpFake() as smtp:
        sender = OutboxSender(factory, _mailer(smtp.port), BASE, KEY, pref_lookup=never)
        assert await sender.run_once() == Report(sent=1)
    [mail] = smtp.to(admin)
    assert mail.message["List-Unsubscribe"] is None
    assert "/api/unsubscribe" not in mail.text


def test_the_sender_refuses_a_key_that_is_not_derived() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        OutboxSender(None, None, BASE, b"short")  # type: ignore[arg-type]
