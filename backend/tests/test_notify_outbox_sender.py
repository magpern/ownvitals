"""Web's outbox sender (ADR 0007 section 2, #194), end to end against the migrated schema and the
in-process SMTP fake. Synthetic accounts (``example.test``) and text only; no health data.

- an owned row goes to its account's address, an owner notice to every active admin, each with
  the kind's fixed text and a link to ``public_base_url`` plus the route
  (``test_a_user_mail_goes_to_the_account_with_the_fixed_text``,
  ``test_an_owner_notice_goes_to_every_active_admin``);
- no one to send to fails the row; a failed delivery retries it
  (``test_a_disabled_account_s_mail_is_failed_not_sent``, ``test_a_failed_delivery_is_retried``);
- the worker cannot send: the sender refuses before it claims anything and no SMTP connection is
  made (``test_the_sender_refuses_in_the_worker_before_claiming``);
- a user's mail uses the table of the account's stored language and says so in
  ``Content-Language``; ``NULL``, a code switched off or one outside the manifest is ``en``, and
  owner notices are ``en`` whatever the admins' languages (ADR 0015 section 7, #253;
  ``test_*language*``);
- the needs-reconnect path of PLAN section 4.3 queues the owner notice in its own transaction
  (``test_giving_up_a_refresh_queues_the_owner_notice``).
"""

import socket
import urllib.request
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ctx import make_context
from dbtools import migrated
from jobtools import admin_url
from ownvitals import locales
from ownvitals.auth import recipients
from ownvitals.config import Settings
from ownvitals.connections.providers.withings import give_up
from ownvitals.egress import OutboundBlocked, core, install_worker_allowlist
from ownvitals.mail import Mailer
from ownvitals.notify import sender as sender_module
from ownvitals.notify.sender import OutboxSender, Report
from ownvitals.notify.templates import LINK, TABLES
from smtpfake import SmtpFake

BASE = "https://ownvitals.example.test"
Factory = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that makes this process the worker hands neither the opener nor the flag on."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture(scope="module", name="outbox_schema")
def outbox_schema_fixture(role_urls: object) -> Iterator[None]:
    with migrated(role_urls.owner):  # type: ignore[attr-defined]
        yield


@pytest.fixture(name="su")
def su_fixture(outbox_schema: None) -> Iterator[Connection]:
    engine = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("TRUNCATE mail_outbox, owner_notices"))
            connection.execute(text("UPDATE users SET role = 'user'"))  # no admin from before
            yield connection
    finally:
        engine.dispose()


@pytest.fixture(name="factory")
async def factory_fixture(outbox_schema: None, app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_async_engine(app_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _mailer(port: int, total_seconds: float | None = None) -> Mailer:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_tls="none",
        smtp_sender="ownvitals@example.test",
        public_base_url=BASE,
    )
    return Mailer(settings, total_seconds=total_seconds)


def _account(
    su: Connection, *, role: str = "user", disabled: bool = False, locale: str | None = None
) -> tuple[uuid.UUID, str]:
    user = uuid.uuid4()
    email = f"synthetic-{user.hex[:12]}@example.test"
    su.execute(
        text(
            "INSERT INTO users "
            "(id, email, email_normalized, email_verified_at, role, disabled_at, locale) "
            "VALUES (:id, :e, :e, now(), :r, CASE WHEN :d THEN now() END, :l)"
        ),
        {"id": user, "e": email, "r": role, "d": disabled, "l": locale},
    )
    return user, email


def _mail_for(su: Connection, owner: uuid.UUID, kind: str = "findings_waiting") -> uuid.UUID:
    route = "findings" if kind == "findings_waiting" else "connections"
    return su.execute(
        text("INSERT INTO mail_outbox (owner_id, kind, route) VALUES (:o, :k, :r) RETURNING id"),
        {"o": owner, "k": kind, "r": route},
    ).scalar_one()


def _states(su: Connection) -> list[tuple[str, str, int]]:
    rows = su.execute(
        text(
            "SELECT kind, state, attempts FROM mail_outbox UNION ALL "
            "SELECT kind, state, attempts FROM owner_notices ORDER BY 1"
        )
    ).all()
    return [(str(k), str(s), int(a)) for k, s, a in rows]


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_a_user_mail_goes_to_the_account_with_the_fixed_text(
    su: Connection, factory: Factory
) -> None:
    alice, address = _account(su)
    row = _mail_for(su, alice)
    async with SmtpFake() as smtp:
        report = await OutboxSender(factory, _mailer(smtp.port), BASE).run_once()
    assert report == Report(sent=1)
    [mail] = smtp.messages
    assert mail.to == address
    assert mail.subject == "ownvitals: something is waiting for you"
    assert f"{BASE}/findings" in mail.text
    # Nothing from the row or the account but the address it went to.
    for leaked in (str(alice), alice.hex, str(row), row.hex, "findings_waiting"):
        assert leaked not in mail.text
        assert leaked not in mail.subject
    assert _states(su) == [("findings_waiting", "sent", 1)]


async def test_an_owner_notice_goes_to_every_active_admin(su: Connection, factory: Factory) -> None:
    _, first = _account(su, role="admin")
    _, second = _account(su, role="admin")
    _account(su, role="admin", disabled=True)
    _, user = _account(su)
    su.execute(text("SELECT owner_notice_queue('connection_needs_reconnect')"))
    async with SmtpFake() as smtp:
        report = await OutboxSender(factory, _mailer(smtp.port), BASE).run_once()
    assert report == Report(sent=1)
    assert sorted(m.to for m in smtp.messages) == sorted([first, second])
    assert smtp.to(user) == []
    for mail in smtp.messages:
        assert f"{BASE}/admin" in mail.text
        assert "connection_needs_reconnect" not in mail.text
    assert _states(su) == [("connection_needs_reconnect", "sent", 1)]


async def test_an_owner_notice_with_no_admin_is_failed(su: Connection, factory: Factory) -> None:
    su.execute(text("SELECT owner_notice_queue('connection_needs_reconnect')"))
    async with SmtpFake() as smtp:
        report = await OutboxSender(factory, _mailer(smtp.port), BASE).run_once()
    assert (report, smtp.messages) == (Report(failed=1), [])
    assert _states(su) == [("connection_needs_reconnect", "failed", 1)]


async def test_a_disabled_account_s_mail_is_failed_not_sent(
    su: Connection, factory: Factory
) -> None:
    gone, _ = _account(su, disabled=True)
    _mail_for(su, gone)
    async with SmtpFake() as smtp:
        report = await OutboxSender(factory, _mailer(smtp.port), BASE).run_once()
    assert (report, smtp.messages) == (Report(failed=1), [])
    assert _states(su) == [("findings_waiting", "failed", 1)]


async def test_a_failed_delivery_is_retried(
    su: Connection, factory: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    alice, address = _account(su)
    _mail_for(su, alice)
    sender = OutboxSender(factory, _mailer(_closed_port(), total_seconds=2), BASE)
    assert await sender.run_once() == Report(retried=1)
    assert _states(su) == [("findings_waiting", "pending", 1)]
    assert await sender.run_once() == Report()  # held before the next attempt
    assert address not in caplog.text


async def test_the_batch_is_bounded(su: Connection, factory: Factory) -> None:
    for _ in range(3):
        _mail_for(su, _account(su)[0])
    async with SmtpFake() as smtp:
        sender = OutboxSender(factory, _mailer(smtp.port), BASE, batch=2)
        assert await sender.run_once() == Report(sent=2)
        assert await sender.run_once() == Report(sent=1)
    assert len(smtp.messages) == 3


async def test_the_sender_refuses_in_the_worker_before_claiming(
    su: Connection, factory: Factory
) -> None:
    alice, _ = _account(su)
    _mail_for(su, alice)
    su.execute(text("SELECT owner_notice_queue('connection_needs_reconnect')"))
    async with SmtpFake() as smtp:
        mailer = _mailer(smtp.port)
        install_worker_allowlist()
        with pytest.raises(OutboundBlocked):
            await OutboxSender(factory, mailer, BASE).run_once()
    assert (smtp.connections, smtp.messages) == (0, [])
    # Nothing was claimed: both rows wait for web, untouched.
    assert _states(su) == [
        ("connection_needs_reconnect", "pending", 0),
        ("findings_waiting", "pending", 0),
    ]


async def test_start_without_an_smtp_host_leaves_mail_queued(factory: Factory) -> None:
    unconfigured = Mailer(Settings())  # pyright: ignore[reportCallIssue]
    assert sender_module.start(factory, unconfigured, BASE) is None
    await sender_module.stop(None)


async def test_start_runs_a_task_that_stop_ends(factory: Factory) -> None:
    task = sender_module.start(factory, _mailer(_closed_port()), BASE)
    assert task is not None
    assert not task.done()
    await sender_module.stop(task)
    assert task.cancelled()


async def test_giving_up_a_refresh_queues_the_owner_notice(
    su: Connection, factory: Factory
) -> None:
    """PLAN section 4.3 step 4: needs_reconnect, the in-app notice and the owner notice in the
    outbox, in one transaction (the worker opens no SMTP connection)."""
    alice, _ = _account(su)
    connection = su.execute(
        text(
            "INSERT INTO connections (owner_id, provider, status) "
            "VALUES (:o, 'test', 'needs_reconnect') RETURNING id"
        ),
        {"o": alice},
    ).scalar_one()
    install_worker_allowlist()
    ctx = make_context(alice)
    assert await give_up(factory, ctx, connection, 0)
    assert await give_up(factory, ctx, connection, 0)  # a second give-up is coalesced
    assert not await give_up(factory, ctx, connection, 7)  # a stale generation queues nothing
    assert _states(su) == [("connection_needs_reconnect", "pending", 0)]
    route = su.execute(text("SELECT route FROM owner_notices")).scalar_one()
    assert route == "admin"


# --- the recipient's language (ADR 0015 section 7, #253) ---------------------------------------


@pytest.mark.parametrize(
    ("stored", "enabled", "expected"),
    [("sv", ("en", "sv"), "sv"), (None, ("en", "sv"), "en"), ("sv", ("en",), "en")],
)
async def test_a_user_mail_is_in_the_account_s_language(
    su: Connection, factory: Factory, stored: str | None, enabled: tuple[str, ...], expected: str
) -> None:
    alice, address = _account(su, locale=stored)
    _mail_for(su, alice)
    async with SmtpFake() as smtp:
        sender = OutboxSender(factory, _mailer(smtp.port), BASE, enabled_locales=enabled)
        assert await sender.run_once() == Report(sent=1)
    [mail] = smtp.to(address)
    assert mail.message["Content-Language"] == expected
    template = TABLES[expected]["findings_waiting"]
    assert mail.subject == template.subject
    expected_body = template.body.replace(LINK, f"{BASE}/findings")
    assert mail.text.replace("\r\n", "\n").rstrip("\n") == expected_body.rstrip("\n")


async def test_owner_notices_are_english_whatever_the_admins_languages(
    su: Connection, factory: Factory
) -> None:
    _, first = _account(su, role="admin", locale="sv")
    _, second = _account(su, role="admin", locale="sv")
    su.execute(text("SELECT owner_notice_queue('connection_needs_reconnect')"))
    async with SmtpFake() as smtp:
        assert await OutboxSender(factory, _mailer(smtp.port), BASE).run_once() == Report(sent=1)
    assert sorted(m.to for m in smtp.messages) == sorted([first, second])
    for mail in smtp.messages:
        assert mail.message["Content-Language"] == "en"
        assert mail.subject == TABLES["en"]["connection_needs_reconnect"].subject


async def test_the_language_lookup_returns_only_a_manifest_code(
    su: Connection, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored value the manifest does not list gives ``en`` (guard 8's lookup never echoes it):
    with ``sv`` taken out of the manifest, an account storing ``sv`` is mailed in English."""
    alice, _ = _account(su, locale="sv")
    assert await recipients.user_locale(factory, alice) == "sv"
    monkeypatch.setattr(locales, "SUPPORTED_LOCALES", ("en",))
    assert await recipients.user_locale(factory, alice) == "en"
    assert await recipients.user_locale(factory, alice, ("en", "sv")) == "en"
    assert await recipients.user_locale(factory, uuid.uuid4()) == "en"  # no account


async def test_the_sender_passes_render_only_the_code(
    su: Connection, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing from the row or the account but the code reaches ``render``."""
    alice, _ = _account(su, locale="sv")
    _mail_for(su, alice)
    seen: list[tuple[object, ...]] = []
    real = sender_module.render

    def spy(*args: object, **kwargs: object) -> object:
        seen.append((*args, *kwargs.values()))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sender_module, "render", spy)
    async with SmtpFake() as smtp:
        assert await OutboxSender(factory, _mailer(smtp.port), BASE).run_once() == Report(sent=1)
    assert seen == [("findings_waiting", "findings", BASE, "sv")]
