"""The mail outbox's tables (ADR 0007 section 1, #194): what a row may hold, who may write it, and
row-level security on the owned table. Synthetic users (``example.test``) only; no health data.

ADR 0007's decisions and their tests:

- a row holds a fixed kind, its recipient and a fixed route, never an address or free text
  (``test_a_row_cannot_hold_an_address_or_free_text``, ``test_the_columns_are_exactly_these``);
- the kinds and routes the database admits are the code's (``test_the_database_admits_exactly_
  the_code_s_kinds_and_routes``);
- the system table admits only owner-notice kinds (``test_the_system_table_admits_only_owner_
  notice_kinds``) and the app role has no privilege on it (``test_the_app_role_has_no_privilege_
  on_the_system_table``; guard 2 checks the grants too);
- a job cannot queue mail for another user (``test_a_session_cannot_queue_mail_for_another_
  user``), and each owner sees only their own rows (``test_each_owner_sees_only_their_own_rows``;
  every execution context through ``contexttools.ROWS``);
- a second pending mail of one kind for one owner is coalesced
  (``test_a_second_pending_mail_of_a_kind_is_coalesced``).

The definer functions are ``test_notify_outbox_functions.py``'s; the sender and templates have
their own files.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ctx import make_context
from dbtools import migrated
from jobtools import admin_url, as_user, ensure_user
from ownvitals.db import user_session
from ownvitals.notify.outbox import (
    OUTBOX_TABLES,
    OWNER_NOTICE_KINDS,
    OWNER_NOTICE_ROUTES,
    ROUTE_PATHS,
    USER_KINDS,
    USER_ROUTES,
    queue_user_mail,
)

REFUSED = "42501"  # insufficient privilege: a missing grant or a row-level security refusal


@pytest.fixture(scope="module", name="outbox_schema")
def outbox_schema_fixture(role_urls: object) -> Iterator[None]:
    with migrated(role_urls.owner):  # type: ignore[attr-defined]
        yield


@pytest.fixture(name="su")
def su_fixture(outbox_schema: None) -> Iterator[Connection]:
    """An autocommit superuser connection on empty outbox tables."""
    engine = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("TRUNCATE mail_outbox, owner_notices"))
            yield connection
    finally:
        engine.dispose()


@pytest.fixture(name="app")
def app_fixture(outbox_schema: None, app_database_url: str) -> Iterator[Engine]:
    engine = create_engine(app_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(name="factory")
async def factory_fixture(
    outbox_schema: None, app_database_url: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(app_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _user(su: Connection) -> uuid.UUID:
    user = uuid.uuid4()
    ensure_user(su, user)
    return user


def _check_values(su: Connection, constraint: str) -> set[str]:
    """The quoted literals of one CHECK constraint, as Postgres stores it."""
    definition = su.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :c"),
        {"c": constraint},
    ).scalar_one()
    return set(definition.split("'")[1::2])


def test_the_tables_are_the_two_of_the_adr(su: Connection) -> None:
    found = su.execute(
        text("SELECT tablename FROM pg_tables WHERE tablename = ANY(:t)"),
        {"t": list(OUTBOX_TABLES)},
    ).scalars()
    assert sorted(found) == sorted(OUTBOX_TABLES)


def test_the_columns_are_exactly_these(su: Connection) -> None:
    """A kind, a recipient, a route key and the send state: no address, subject or text."""
    state = ["state", "attempts", "hold_until", "created_at", "updated_at", "sent_at"]
    for table, expected in {
        "mail_outbox": ["id", "owner_id", "kind", "route", *state],
        "owner_notices": ["id", "kind", "route", *state],
    }.items():
        columns = su.execute(
            text(
                "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:t AS regclass) "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
            ),
            {"t": f"public.{table}"},
        ).scalars()
        assert list(columns) == expected


def test_the_database_admits_exactly_the_code_s_kinds_and_routes(su: Connection) -> None:
    assert _check_values(su, "mail_outbox_kind") == set(USER_KINDS)
    assert _check_values(su, "mail_outbox_route") == set(USER_ROUTES)
    assert _check_values(su, "owner_notices_kind") == set(OWNER_NOTICE_KINDS)
    assert _check_values(su, "owner_notices_route") == set(OWNER_NOTICE_ROUTES.values())
    assert set(OWNER_NOTICE_ROUTES) == set(OWNER_NOTICE_KINDS)
    assert set(ROUTE_PATHS) == {*USER_ROUTES, *OWNER_NOTICE_ROUTES.values()}
    assert not set(USER_KINDS) & set(OWNER_NOTICE_KINDS)
    # An owned row never links to the admin page.
    assert not set(USER_ROUTES) & set(OWNER_NOTICE_ROUTES.values())


@pytest.mark.parametrize(
    ("kind", "route"),
    [
        ("findings_waiting", "https://evil.example/phish"),  # a URL, not a route key
        ("findings_waiting", "someone@example.com"),  # an address
        ("Your weight is 81.4 kg", "findings"),  # free text in the kind
        ("findings_waiting", "admin"),  # the admin page is not a user's route
        ("connection_needs_reconnect", "admin"),  # an owner-notice kind on an owned row
        ("findings_waiting", "Findings"),  # keys are exact
    ],
)
def test_a_row_cannot_hold_an_address_or_free_text(su: Connection, kind: str, route: str) -> None:
    user = _user(su)
    with pytest.raises(IntegrityError, match="violates check constraint"):
        su.execute(
            text("INSERT INTO mail_outbox (owner_id, kind, route) VALUES (:o, :k, :r)"),
            {"o": user, "k": kind, "r": route},
        )


@pytest.mark.parametrize(
    ("kind", "route"),
    [
        ("findings_waiting", "findings"),  # a user-facing template never goes to the admins
        ("connection_attention", "admin"),
        ("connection_needs_reconnect", "findings"),
        ("connection_needs_reconnect", "https://evil.example/"),
    ],
)
def test_the_system_table_admits_only_owner_notice_kinds(
    su: Connection, kind: str, route: str
) -> None:
    with pytest.raises(IntegrityError, match="violates check constraint"):
        su.execute(
            text("INSERT INTO owner_notices (kind, route) VALUES (:k, :r)"),
            {"k": kind, "r": route},
        )
    su.execute(
        text(
            "INSERT INTO owner_notices (kind, route) VALUES ('connection_needs_reconnect', 'admin')"
        )
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM owner_notices",
        "SELECT count(*) FROM owner_notices",
        "INSERT INTO owner_notices (kind, route) VALUES ('connection_needs_reconnect', 'admin')",
        "UPDATE owner_notices SET state = 'sent'",
        "DELETE FROM owner_notices",
        "TRUNCATE owner_notices",
    ],
)
def test_the_app_role_has_no_privilege_on_the_system_table(app: Engine, statement: str) -> None:
    with app.connect() as connection, pytest.raises(DBAPIError) as raised:
        connection.execute(text(statement))
    assert getattr(raised.value.orig, "sqlstate", None) == REFUSED


def test_the_owned_table_has_forced_row_level_security(su: Connection) -> None:
    row = su.execute(
        text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = CAST('public.mail_outbox' AS regclass)"
        )
    ).one()
    assert tuple(row) == (True, True)


def test_each_owner_sees_only_their_own_rows(su: Connection, app: Engine) -> None:
    alice, bob = _user(su), _user(su)
    for owner in (alice, bob):
        su.execute(
            text(
                "INSERT INTO mail_outbox (owner_id, kind, route) "
                "VALUES (:o, 'findings_waiting', 'findings')"
            ),
            {"o": owner},
        )
    with app.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM mail_outbox")).scalar_one() == 0
        as_user(connection, alice)
        owners = connection.execute(text("SELECT owner_id FROM mail_outbox")).scalars().all()
        assert owners == [alice]
        changed = connection.execute(
            text("UPDATE mail_outbox SET state = 'sent' WHERE owner_id = :b"), {"b": bob}
        ).rowcount
        assert changed == 0


async def test_a_session_cannot_queue_mail_for_another_user(
    su: Connection, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A job writes in its owner's session (a fenced ``user_session``): the policy's WITH CHECK
    refuses a row for anyone else (ADR 0007 section 1)."""
    alice, bob = _user(su), _user(su)
    with pytest.raises(DBAPIError) as raised:
        async with user_session(factory, make_context(alice)) as db:
            await queue_user_mail(db, bob, "findings_waiting", "findings")
    assert getattr(raised.value.orig, "sqlstate", None) == REFUSED
    async with user_session(factory, make_context(alice)) as db:
        assert await queue_user_mail(db, alice, "findings_waiting", "findings")
    owners = su.execute(text("SELECT owner_id FROM mail_outbox")).scalars().all()
    assert owners == [alice]


async def test_a_second_pending_mail_of_a_kind_is_coalesced(
    su: Connection, factory: async_sessionmaker[AsyncSession]
) -> None:
    alice = _user(su)
    ctx = make_context(alice)
    async with user_session(factory, ctx) as db:
        assert await queue_user_mail(db, alice, "findings_waiting", "findings")
        assert not await queue_user_mail(db, alice, "findings_waiting", "findings")
        assert await queue_user_mail(db, alice, "connection_attention", "connections")
    assert su.execute(text("SELECT count(*) FROM mail_outbox")).scalar_one() == 2
    su.execute(text("UPDATE mail_outbox SET state = 'sent'"))
    async with user_session(factory, ctx) as db:  # once sent, the next one is a new row
        assert await queue_user_mail(db, alice, "findings_waiting", "findings")


async def test_the_write_refuses_a_kind_or_route_outside_the_lists(
    su: Connection, factory: async_sessionmaker[AsyncSession]
) -> None:
    alice = _user(su)
    async with user_session(factory, make_context(alice)) as db:
        with pytest.raises(ValueError, match="not a user-facing"):
            await queue_user_mail(db, alice, "connection_needs_reconnect", "findings")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="not a user-facing"):
            await queue_user_mail(db, alice, "findings_waiting", "admin")  # type: ignore[arg-type]


def test_deleting_the_account_deletes_its_mail(su: Connection) -> None:
    alice = _user(su)
    su.execute(
        text(
            "INSERT INTO mail_outbox (owner_id, kind, route) "
            "VALUES (:o, 'findings_waiting', 'findings')"
        ),
        {"o": alice},
    )
    su.execute(text("DELETE FROM users WHERE id = :o"), {"o": alice})
    assert su.execute(text("SELECT count(*) FROM mail_outbox")).scalar_one() == 0
