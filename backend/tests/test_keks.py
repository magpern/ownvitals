"""The key ring on a database: the ``connection_keks`` check, the seed and pepper slots, retired
keys and the startup refusals (ADR 0004 sections 3 and 4). The key files themselves are
``test_keks_files.py``. Every key here is synthetic test material."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from conftest import RoleUrls
from dbtools import migrated
from keystools import (
    KEY_A,
    KEY_B,
    KEY_C,
    KEY_D,
    app_factory,
    check_rows,
    clear_check_rows,
    insert_check_row,
    rotate,
    settings_for,
    write_key,
)
from ownvitals.crypto import keks
from ownvitals.crypto.envelope import (
    Kek,
    RingKey,
    WebhookPepper,
    WebhookSeed,
    check_value,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
    seal,
)
from ownvitals.db import system_session
from ownvitals.web import create_app

KINDS: list[tuple[type[RingKey], str, str]] = [
    (Kek, "connection_kek", "kek-1"),
    (WebhookSeed, "webhook_seed", "seed-1"),
    (WebhookPepper, "webhook_pepper", "pep-1"),
]


@pytest.fixture(scope="module")
def keks_schema(role_urls: RoleUrls) -> Iterator[None]:
    """The migrated schema for this module's database tests."""
    with migrated(role_urls.owner):
        yield


@pytest.fixture
def db(keks_schema: None, database_url: str, app_database_url: str) -> str:
    """No check rows yet; returns the app role's URL."""
    clear_check_rows(database_url)
    return app_database_url


async def load(app_url: str, role: keks.Role, **overrides: object) -> keks.KeyRing:
    """A fresh process's ring from the test's secrets directory (it records the check rows)."""
    settings = settings_for(app_url, **overrides)
    async with app_factory(settings) as factory:
        return await keks.load_keyring(factory, settings, role=role)


async def test_startup_records_each_check_value_once(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    write_key(secrets_dir, "connection_kek", KEY_A)
    write_key(secrets_dir, "webhook_pepper", KEY_B)
    write_key(secrets_dir, "webhook_seed", KEY_C)
    ring = await load(db, "worker")
    assert ring.verified
    assert (ring.current_id, ring.pepper_id, ring.seed_id) == ("kek-1", "pep-1", "seed-1")
    expected = {
        "kek-1": check_value(Kek("kek-1", KEY_A)),
        "pep-1": check_value(WebhookPepper("pep-1", KEY_B)),
        "seed-1": check_value(WebhookSeed("seed-1", KEY_C)),
    }
    assert check_rows(database_url) == expected
    for key in (KEY_A, KEY_B, KEY_C):
        assert all(key not in value for value in expected.values())
    await load(db, "worker")  # a second process: the same rows
    (secrets_dir / "webhook_seed").unlink()  # web's secrets never hold the seed
    assert (await load(db, "web")).seed_id is None
    assert check_rows(database_url) == expected


async def test_web_holds_the_kek_and_the_pepper_and_never_the_seed(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    write_key(secrets_dir, "webhook_pepper", KEY_B)
    ring = await load(db, "web")
    assert (ring.role, ring.pepper_id, ring.seed_id) == ("web", "pep-1", None)
    assert set(check_rows(database_url)) == {"kek-1", "pep-1"}
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        assert (await ring.current_pepper(session)).key == KEY_B
        assert (await ring.pepper_for(session, "pep-1")).key == KEY_B
        with pytest.raises(keks.SeedRefused):
            await ring.current_seed(session)
        with pytest.raises(keks.SeedRefused):
            await ring.seed_for(session, "seed-1")


@pytest.mark.parametrize("name", ["webhook_seed", "webhook_seed_seed-0"])
async def test_web_refuses_to_start_when_it_can_see_a_seed_file(
    db: str, database_url: str, secrets_dir: Path, name: str
) -> None:
    write_key(secrets_dir, name, KEY_C)
    with pytest.raises(keks.KeyRingError, match="web must never hold the webhook seed"):
        await load(db, "web")
    assert check_rows(database_url) == {}  # refused before the database
    app = create_app(settings_for(db))
    with pytest.raises(keks.KeyRingError, match="web must never hold the webhook seed"):
        async with app.router.lifespan_context(app):
            pytest.fail("web must not start")
    ring = await load(db, "worker")  # the worker may hold it
    assert ring.seed_id == ("seed-1" if name == "webhook_seed" else None)


async def test_web_refuses_to_start_on_a_swapped_missing_or_malformed_kek(
    db: str, secrets_dir: Path
) -> None:
    app = create_app(settings_for(db))
    async with app.router.lifespan_context(app):  # records kek-1's check row
        assert app.state.keyring.verified
    write_key(secrets_dir, "connection_kek", KEY_D)  # another key under the same id
    app = create_app(settings_for(db))
    with pytest.raises(keks.KeyRingError, match="does not match the check value"):
        async with app.router.lifespan_context(app):
            pytest.fail("web must not start")
    (secrets_dir / "connection_kek").write_bytes(KEY_D.hex().upper().encode())
    app = create_app(settings_for(db))
    with pytest.raises(keks.KeyRingError, match="64 lowercase hexadecimal"):
        async with app.router.lifespan_context(app):
            pytest.fail("web must not start")
    (secrets_dir / "connection_kek").unlink()
    app = create_app(settings_for(db))
    with pytest.raises(keks.KeyRingError, match="connection_kek secret file is missing"):
        async with app.router.lifespan_context(app):
            pytest.fail("web must not start")


@pytest.mark.parametrize(("cls", "name", "key_id"), KINDS)
async def test_the_worker_refuses_to_start_on_a_swapped_key_of_any_kind(
    db: str, secrets_dir: Path, cls: type[RingKey], name: str, key_id: str
) -> None:
    write_key(secrets_dir, "connection_kek", KEY_A)
    write_key(secrets_dir, "webhook_pepper", KEY_B)
    write_key(secrets_dir, "webhook_seed", KEY_C)
    await load(db, "worker")
    write_key(secrets_dir, name, KEY_D)
    with pytest.raises(keks.KeyRingError, match=f"the {name} file does not match the check"):
        await load(db, "worker")


@pytest.mark.parametrize(("role", "name"), [("web", "webhook_pepper"), ("worker", "webhook_seed")])
async def test_a_present_but_malformed_seed_or_pepper_refuses_to_start(
    db: str, database_url: str, secrets_dir: Path, role: keks.Role, name: str
) -> None:
    (secrets_dir / name).write_bytes(b"not a key\n")
    with pytest.raises(keks.KeyRingError, match=f"the {name} secret file must hold"):
        await load(db, role)
    assert check_rows(database_url) == {}


@pytest.mark.parametrize(
    ("role", "files"),
    [
        ("web", {"webhook_pepper": KEY_A}),  # the pepper is the KEK
        ("worker", {"webhook_seed": KEY_A}),  # the seed is the KEK
        ("worker", {"webhook_pepper": KEY_B, "webhook_seed": KEY_B}),  # seed and pepper alike
    ],
)
async def test_two_kinds_sharing_one_key_refuse_to_start(
    db: str, database_url: str, secrets_dir: Path, role: keks.Role, files: dict[str, bytes]
) -> None:
    write_key(secrets_dir, "connection_kek", KEY_A)
    for name, key in files.items():
        write_key(secrets_dir, name, key)
    with pytest.raises(keks.KeyRingError, match="hold the same key"):
        await load(db, role)
    assert check_rows(database_url) == {}


async def test_an_unreachable_database_defers_the_check_to_first_use(
    unreachable_database_url: str, db: str, database_url: str, secrets_dir: Path
) -> None:
    unreachable = create_app(settings_for(unreachable_database_url))
    async with unreachable.router.lifespan_context(unreachable):
        assert unreachable.state.keyring.verified is False
    deferred = await load(unreachable_database_url, "worker")
    assert deferred.verified is False
    # On a reachable database with a swapped key the first use refuses, every time.
    insert_check_row(database_url, "kek-1", check_value(Kek("kek-1", KEY_D)))
    write_key(secrets_dir, "connection_kek", KEY_A)
    async with app_factory(settings_for(db)) as factory:
        kek = keks.read_current_kek("kek-1", secrets_dir)
        ring = keks.KeyRing("worker", secrets_dir, kek, verified=False, factory=factory)
        async with system_session(factory) as session:
            for _ in range(2):
                with pytest.raises(keks.KeyRingError, match="does not match the check value"):
                    await ring.current(session)
                with pytest.raises(keks.KeyRingError):
                    await ring.kek_for(session, "kek-1")
        assert ring.verified is False
        # With the right key the first use checks, records nothing new and verifies.
        clear_check_rows(database_url)
        async with system_session(factory) as session:
            assert (await ring.current(session)).key == KEY_A
        assert ring.verified
        assert check_rows(database_url) == {"kek-1": check_value(Kek("kek-1", KEY_A))}


async def test_a_rolled_back_caller_does_not_undo_the_deferred_check(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    """The first-use check runs in its own transaction: the check row is committed and the ring
    verified even when the caller's transaction rolls back afterwards."""

    class Rollback(Exception):
        pass

    async with app_factory(settings_for(db)) as factory:
        kek = keks.read_current_kek("kek-1", secrets_dir)
        ring = keks.KeyRing("web", secrets_dir, kek, verified=False, factory=factory)

        async def use_then_roll_back() -> None:
            async with system_session(factory) as session:
                await ring.current(session)
                raise Rollback

        with pytest.raises(Rollback):
            await use_then_roll_back()
    assert ring.verified
    assert set(check_rows(database_url)) == {"kek-1"}


async def test_retired_keks_load_only_with_a_matching_check_row(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    ring = await load(db, "worker")
    # kek-0 was current once (it has a row); kek-1 is current now.
    insert_check_row(database_url, "kek-0", check_value(Kek("kek-0", KEY_B)))
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        with pytest.raises(keks.KeyUnavailable) as raised:  # no file yet
            await ring.kek_for(session, "kek-0")
        assert raised.value.args == ("key_unavailable",)
        write_key(secrets_dir, "connection_kek_kek-0", KEY_C)  # another key under that id
        with pytest.raises(keks.KeyUnavailable):
            await ring.kek_for(session, "kek-0")
        (secrets_dir / "connection_kek_kek-0").write_bytes(b"short\n")  # malformed
        with pytest.raises(keks.KeyUnavailable):
            await ring.kek_for(session, "kek-0")
        write_key(secrets_dir, "connection_kek_kek-0", KEY_B)
        assert (await ring.kek_for(session, "kek-0")).key == KEY_B
        write_key(secrets_dir, "connection_kek_kek-7", KEY_D)  # a file with no row
        with pytest.raises(keks.KeyUnavailable):
            await ring.kek_for(session, "kek-7")
        with pytest.raises(keks.KeyUnavailable):  # an id nothing knows
            await ring.kek_for(session, "kek-9")
        for bad in ("../connection_kek", "app-v1", "kek-1/x", "", "seed-0", "pep-0", "kek-0\n"):
            with pytest.raises(keks.KeyUnavailable):
                await ring.kek_for(session, bad)
    assert KEY_B.hex()[:8] not in repr(ring)


async def test_a_retired_kek_still_opens_what_it_sealed(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    """Rotation (ADR 0004 section 3): the old current file becomes ``connection_kek_<old id>``;
    rows sealed under it open until it is removed, new writes use the new KEK."""
    owner, row = uuid.uuid4(), uuid.uuid4()
    payload = b'{"access_token":"synthetic"}'
    write_key(secrets_dir, "connection_kek", KEY_A)
    old_ring = await load(db, "web")
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        old = await old_ring.current(session)
    sealed = seal(
        payload,
        connection_aad(owner, "withings", row),
        old,
        connection_wrap_aad(old.key_id, owner, "withings", row),
    )
    rotate(secrets_dir, "kek-2", KEY_B, "kek-1")
    ring = await load(db, "web", connection_kek_id="kek-2")
    assert ring.current_id == "kek-2"
    assert set(check_rows(database_url)) == {"kek-1", "kek-2"}
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        retired = await ring.kek_for(session, sealed.key_id)
        assert (retired.key_id, retired.key) == ("kek-1", KEY_A)
        assert (
            open_sealed(
                sealed.ciphertext,
                sealed.nonce,
                sealed.wrapped_dek,
                sealed.dek_nonce,
                retired,
                connection_aad(owner, "withings", row),
                connection_wrap_aad("kek-1", owner, "withings", row),
            )
            == payload
        )
        assert (await ring.current(session)).key == KEY_B
    (secrets_dir / "connection_kek_kek-1").unlink()  # the runbook's last step
    fresh = await load(db, "web", connection_kek_id="kek-2")
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        with pytest.raises(keks.KeyUnavailable):
            await fresh.kek_for(session, "kek-1")


async def test_retired_peppers_and_seeds_stay_within_their_kind(
    db: str, database_url: str, secrets_dir: Path
) -> None:
    write_key(secrets_dir, "webhook_pepper", KEY_B)
    write_key(secrets_dir, "webhook_seed", KEY_C)
    ring = await load(db, "worker")
    insert_check_row(database_url, "pep-0", check_value(WebhookPepper("pep-0", KEY_D)))
    insert_check_row(database_url, "seed-0", check_value(WebhookSeed("seed-0", KEY_D)))
    write_key(secrets_dir, "webhook_pepper_pep-0", KEY_D)
    write_key(secrets_dir, "webhook_seed_seed-0", KEY_D)
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        pepper = await ring.pepper_for(session, "pep-0")
        seed = await ring.seed_for(session, "seed-0")
        assert (type(pepper), pepper.key) == (WebhookPepper, KEY_D)
        assert (type(seed), seed.key) == (WebhookSeed, KEY_D)
        assert (await ring.current_seed(session)).key == KEY_C
        assert (await ring.seed_for(session, "seed-1")).key == KEY_C
        # An id of another kind never opens a file: the kinds don't mix.
        for lookup, key_id in (
            (ring.pepper_for, "seed-0"),
            (ring.pepper_for, "kek-1"),
            (ring.seed_for, "pep-0"),
            (ring.seed_for, "kek-1"),
            (ring.kek_for, "pep-0"),
            (ring.kek_for, "seed-0"),
        ):
            with pytest.raises(keks.KeyUnavailable):
                await lookup(session, key_id)
    # Web never opens a retired seed either, even with its file and row in place.
    (secrets_dir / "webhook_seed").unlink()
    (secrets_dir / "webhook_seed_seed-0").unlink()
    web = await load(db, "web")
    write_key(secrets_dir, "webhook_seed_seed-0", KEY_D)  # appears after startup
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        with pytest.raises(keks.SeedRefused):
            await web.seed_for(session, "seed-0")


async def test_an_empty_slot_is_unavailable(db: str, secrets_dir: Path) -> None:
    ring = await load(db, "worker")  # M0: no seed or pepper file installed
    assert (ring.pepper_id, ring.seed_id) == (None, None)
    async with app_factory(settings_for(db)) as factory, system_session(factory) as session:
        with pytest.raises(keks.KeyUnavailable):
            await ring.current_pepper(session)
        with pytest.raises(keks.KeyUnavailable):
            await ring.current_seed(session)
        with pytest.raises(keks.KeyUnavailable):
            await ring.pepper_for(session, "pep-1")


async def test_the_app_role_may_record_and_read_check_rows_but_not_change_or_forget_them(
    db: str, database_url: str
) -> None:
    """Migration 0003 grants ownvitals_app SELECT and INSERT on connection_keks only."""
    value = check_value(Kek("kek-9", KEY_D))
    async with app_factory(settings_for(db)) as factory:
        async with system_session(factory) as session:
            await session.execute(
                text("INSERT INTO connection_keks (key_id, check_value) VALUES ('kek-9', :v)"),
                {"v": value},
            )
        async with system_session(factory) as session:
            stored = await keks.stored_check_value(session, "kek-9")
        assert stored == value
        for statement in (
            "UPDATE connection_keks SET check_value = :v WHERE key_id = 'kek-9'",
            "DELETE FROM connection_keks WHERE key_id = 'kek-9'",
            "TRUNCATE connection_keks",
            "ALTER TABLE connection_keks DROP CONSTRAINT connection_keks_check_value",
        ):
            with pytest.raises(
                (DBAPIError, ProgrammingError), match=r"permission denied|must be owner"
            ):
                async with system_session(factory) as session:
                    await session.execute(text(statement), {"v": check_value(Kek("kek-9", KEY_A))})
    assert check_rows(database_url) == {"kek-9": value}


@pytest.mark.parametrize(
    ("key_id", "value"),
    [
        ("app-v1", bytes(16)),  # not an id of the ring
        ("kek-1/x", bytes(16)),
        ("KEK-1", bytes(16)),
        ("kek-1", bytes(15)),  # a check value is 16 bytes
        ("kek-1", bytes(32)),
    ],
)
def test_the_check_table_refuses_a_foreign_id_or_a_wrong_length(
    db: str, database_url: str, key_id: str, value: bytes
) -> None:
    with pytest.raises(IntegrityError, match="connection_keks_"):
        insert_check_row(database_url, key_id, value)
