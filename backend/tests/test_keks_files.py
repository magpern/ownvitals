"""The key ring's files (ADR 0004 sections 3 and 4): the file format, the ids of each kind and
how a ring is built, without a database. ``test_keks.py`` has the check table and startup.
Every key here is synthetic test material."""

import pickle
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from keystools import KEY_A, KEY_B, KEY_C, key_file, settings_for, write_key
from ownvitals.crypto import keks
from ownvitals.crypto.envelope import Kek, RetiredKek, RingKey, WebhookPepper, WebhookSeed

KINDS: list[tuple[type[RingKey], str, str]] = [
    (Kek, "connection_kek", "kek-1"),
    (WebhookSeed, "webhook_seed", "seed-1"),
    (WebhookPepper, "webhook_pepper", "pep-1"),
]


@pytest.mark.parametrize(("cls", "name", "key_id"), KINDS)
@pytest.mark.parametrize("newline", [True, False])
def test_a_valid_file_is_read(
    tmp_path: Path, cls: type[RingKey], name: str, key_id: str, newline: bool
) -> None:
    (tmp_path / name).write_bytes(key_file(KEY_A, newline=newline))
    key = keks.read_current(cls, key_id, tmp_path)
    assert key is not None
    assert (type(key), key.key_id, key.key) == (cls, key_id, KEY_A)


@pytest.mark.parametrize(("cls", "name", "key_id"), KINDS)
@pytest.mark.parametrize(
    "content",
    [
        KEY_A.hex().upper().encode(),
        KEY_A.hex().encode() + b"\r\n",
        KEY_A.hex().encode() + b"\n\n",
        KEY_A.hex().encode() + b" ",
        KEY_A.hex().encode()[:-1],
        KEY_A.hex().encode() + b"0",
        KEY_A.hex().encode() + b"\n" + b"0" * 1000,
        b" " + KEY_A.hex().encode(),
        b"",
        b"\n",
        KEY_A,  # raw bytes, not hex
    ],
)
def test_a_malformed_file_refuses_without_showing_it(
    tmp_path: Path, cls: type[RingKey], name: str, key_id: str, content: bytes
) -> None:
    (tmp_path / name).write_bytes(content)
    with pytest.raises(keks.KeyRingError, match="64 lowercase hexadecimal") as raised:
        keks.read_current(cls, key_id, tmp_path)
    assert KEY_A.hex() not in str(raised.value).lower()
    assert raised.value.__context__ is None


def test_a_directory_in_place_of_a_key_file_is_malformed(tmp_path: Path) -> None:
    (tmp_path / "connection_kek").mkdir()
    with pytest.raises(keks.KeyRingError, match="64 lowercase hexadecimal"):
        keks.read_current_kek("kek-1", tmp_path)


def test_a_missing_kek_refuses_but_a_missing_seed_or_pepper_is_an_empty_slot(
    tmp_path: Path,
) -> None:
    with pytest.raises(keks.KeyRingError, match="connection_kek secret file is missing"):
        keks.read_current_kek("kek-1", tmp_path)
    assert keks.read_current(WebhookSeed, "seed-1", tmp_path) is None
    assert keks.read_current(WebhookPepper, "pep-1", tmp_path) is None


@pytest.mark.parametrize(
    ("cls", "bad", "setting"),
    [
        (Kek, "app-v1", "connection_kek_id"),
        (Kek, "kek-", "connection_kek_id"),
        (Kek, "kek-UPPER", "connection_kek_id"),
        (Kek, "kek-1/../x", "connection_kek_id"),
        (Kek, "seed-1", "connection_kek_id"),  # an id of another kind
        (WebhookSeed, "kek-1", "webhook_seed_id"),
        (WebhookSeed, "pep-1", "webhook_seed_id"),
        (WebhookPepper, "seed-1", "webhook_pepper_id"),
    ],
)
def test_a_bad_or_foreign_id_refuses(
    tmp_path: Path, cls: type[RingKey], bad: str, setting: str
) -> None:
    for name in ("connection_kek", "webhook_seed", "webhook_pepper"):
        write_key(tmp_path, name, KEY_A)
    with pytest.raises(keks.KeyRingError, match=f"{setting} does not match"):
        keks.read_current(cls, bad, tmp_path)


def test_a_ring_is_built_only_for_web_or_the_worker_and_web_never_with_a_seed(
    tmp_path: Path,
) -> None:
    kek, factory = Kek("kek-1", KEY_A), async_sessionmaker()
    with pytest.raises(keks.SeedRefused):
        keks.KeyRing("web", tmp_path, kek, factory=factory, seed=WebhookSeed("seed-1", KEY_B))
    with pytest.raises(ValueError, match="web or worker"):
        keks.KeyRing("admin", tmp_path, kek, factory=factory)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="current KEK is a Kek"):
        keks.KeyRing("worker", tmp_path, RetiredKek("kek-1", KEY_A), factory=factory)  # pyright: ignore[reportArgumentType]


def test_a_hand_built_ring_starts_unverified(tmp_path: Path) -> None:
    """Only a check against the check rows (``verify``) marks a ring verified; ``test_keks.py``
    shows an unverified one refusing a swapped key and verifying a matching one."""
    ring = keks.KeyRing("worker", tmp_path, Kek("kek-1", KEY_A), factory=async_sessionmaker())
    assert ring.verified is False


async def test_load_keyring_takes_only_web_or_the_worker(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="web or worker"):
        await keks.load_keyring(
            None,  # pyright: ignore[reportArgumentType]
            settings_for("postgresql+psycopg://u@127.0.0.1:1/x"),
            role="maint",  # pyright: ignore[reportArgumentType]
            directory=tmp_path,
        )


def test_a_ring_shows_ids_only_and_cannot_be_pickled(tmp_path: Path) -> None:
    ring = keks.KeyRing(
        "worker",
        tmp_path,
        Kek("kek-1", KEY_A),
        factory=async_sessionmaker(),
        pepper=WebhookPepper("pep-1", KEY_B),
        seed=WebhookSeed("seed-1", KEY_C),
    )
    assert repr(ring) == "KeyRing(role='worker', kek='kek-1', pepper='pep-1', seed='seed-1')"
    for key in (KEY_A, KEY_B, KEY_C):
        assert key.hex() not in repr(ring)
    with pytest.raises(TypeError):
        pickle.dumps(ring)
