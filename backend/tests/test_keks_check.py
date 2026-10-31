"""When the key ring's check runs (ADR 0004 section 3): at startup; deferred only when the
database is unreachable at connect, then on first use and at readiness; and a ring starts
unverified however it was built. Every key here is synthetic test material."""

import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from conftest import RoleUrls
from dbtools import migrated
from keystools import (
    KEY_A,
    KEY_D,
    app_factory,
    check_rows,
    clear_check_rows,
    insert_check_row,
    settings_for,
    write_key,
)
from ownvitals.crypto import keks
from ownvitals.crypto.envelope import Kek, check_value
from ownvitals.db import system_session
from ownvitals.web import create_app

LOCK_NOT_AVAILABLE = "55P03"


@pytest.fixture(scope="module")
def keks_schema(role_urls: RoleUrls) -> Iterator[None]:
    """The migrated schema for this module's database tests."""
    with migrated(role_urls.owner):
        yield


@pytest.fixture
def db(keks_schema: None, database_url: str, app_database_url: str) -> Iterator[str]:
    """No check rows before or after (migration 0003's downgrade refuses while there are any);
    returns the app role's URL."""
    clear_check_rows(database_url)
    yield app_database_url
    clear_check_rows(database_url)


def client_for(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_an_unreachable_database_defers_the_check_and_every_use_refuses(
    unreachable_database_url: str, db: str
) -> None:
    """Only a database unreachable at connect defers the check (the classification of
    ``db.role_check``): web starts, ``/api/ready`` answers 503 and every use refuses."""
    app = create_app(settings_for(unreachable_database_url))
    async with app.router.lifespan_context(app), client_for(app) as client:
        ring = app.state.keyring
        assert ring.verified is False
        assert (await client.get("/api/ready")).status_code == 503
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        for use in (ring.verify(), ring.current(session), ring.kek_for(session, "kek-0")):
            with pytest.raises(keks.KeyRingError, match="not verified yet"):
                await use
    assert ring.verified is False
    async with app_factory(settings_for(unreachable_database_url)) as factory:
        worker = await keks.load_keyring(factory, settings_for(db), role="worker")
    assert worker.verified is False


async def test_a_lock_timeout_during_the_startup_check_refuses_to_start(
    db: str, database_url: str
) -> None:
    """An error once connected refuses startup, a timeout included: here the check waits out
    the app role's ``lock_timeout`` (5 s, ``10-roles.sql``) behind a lock the superuser holds."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as holder:
            holder.execute(text("LOCK TABLE connection_keks IN ACCESS EXCLUSIVE MODE"))
            app = create_app(settings_for(db))
            with pytest.raises(OperationalError) as refused:
                async with app.router.lifespan_context(app):
                    pytest.fail("web must not start")
            async with app_factory(settings_for(db)) as factory:
                with pytest.raises(OperationalError) as worker_refused:
                    await keks.load_keyring(factory, settings_for(db), role="worker")
            holder.rollback()
    finally:
        engine.dispose()
    for error in (refused.value, worker_refused.value):
        assert getattr(error.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE
    assert check_rows(database_url) == {}


async def test_a_hand_built_ring_checks_before_its_first_use(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    """A ring starts unverified however it was built: with a swapped key every use refuses,
    retired lookups included, every time; with the right key the first use verifies it."""
    insert_check_row(database_url, "kek-1", check_value(Kek("kek-1", KEY_D)))
    write_key(secrets_dir, "connection_kek", KEY_A)
    async with app_factory(settings_for(db)) as factory:
        kek = keks.read_current_kek("kek-1", secrets_dir)
        ring = keks.KeyRing("worker", secrets_dir, kek, factory=factory)
        assert ring.verified is False
        async with system_session(factory) as session:
            for _ in range(2):
                for use in (
                    ring.verify(),
                    ring.current(session),
                    ring.kek_for(session, "kek-1"),
                    ring.kek_for(session, "kek-0"),
                    ring.pepper_for(session, "pep-0"),
                ):
                    with pytest.raises(keks.KeyRingError, match="does not match the check value"):
                        await use
        assert ring.verified is False
        # With the right key the first use checks, records nothing new and verifies.
        clear_check_rows(database_url)
        async with system_session(factory) as session:
            assert (await ring.current(session)).key == KEY_A
        assert ring.verified
        assert check_rows(database_url) == {"kek-1": check_value(Kek("kek-1", KEY_A))}


async def test_ready_answers_503_until_the_ring_is_verified(
    db: str, secrets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_key(secrets_dir, "connection_kek", KEY_A)
    app = create_app(settings_for(db))
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await client.get("/api/ready")).status_code == 200
        factory = app.state.session_factory
        # A deferred ring that finds a swapped key when it checks: not ready, every time.
        swapped = keks.KeyRing("web", secrets_dir, Kek("kek-1", KEY_D), factory=factory)
        app.state.keyring = swapped
        with caplog.at_level(logging.WARNING, logger="ownvitals.web"):
            for _ in range(2):
                response = await client.get("/api/ready")
                assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
        assert swapped.verified is False
        assert [r.getMessage() for r in caplog.records].count("key ring not verified") == 2
        assert KEY_D.hex() not in caplog.text
        # A deferred ring with the right key: the readiness probe runs its check.
        deferred = keks.KeyRing("web", secrets_dir, Kek("kek-1", KEY_A), factory=factory)
        app.state.keyring = deferred
        assert (await client.get("/api/ready")).status_code == 200
        assert deferred.verified


async def test_a_rolled_back_caller_does_not_undo_the_deferred_check(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    """The first-use check runs in its own transaction: the check row is committed and the ring
    verified even when the caller's transaction rolls back afterwards."""

    class Rollback(Exception):
        pass

    async with app_factory(settings_for(db)) as factory:
        kek = keks.read_current_kek("kek-1", secrets_dir)
        ring = keks.KeyRing("web", secrets_dir, kek, factory=factory)

        async def use_then_roll_back() -> None:
            async with system_session(factory) as session:
                await ring.current(session)
                raise Rollback

        with pytest.raises(Rollback):
            await use_then_roll_back()
    assert ring.verified
    assert set(check_rows(database_url)) == {"kek-1"}
