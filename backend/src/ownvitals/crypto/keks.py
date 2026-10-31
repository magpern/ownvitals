"""The key ring: KEK files, the webhook seed and pepper slots, and the ``connection_keks`` check
(ADR 0004 sections 3 and 4).

- Each kind of key (``KeyKind``) has a current secret file and retired files named
  ``<file>_<id>``: ``connection_kek``, ``webhook_seed`` and ``webhook_pepper``. A file holds
  exactly 64 lowercase hexadecimal characters (32 bytes), optionally followed by one newline. The
  current ids are settings (``connection_kek_id``, ``webhook_seed_id``, ``webhook_pepper_id``),
  each checked against its kind's pattern; a retired file is named only by a validated id of its
  kind, so no other input builds a file name and an id of one kind never opens another's file.
- ``connection_keks`` (a system table, reached only through this module) holds
  ``HMAC-SHA256(key, "ownvitals-<kind>-check-v1")[:16]`` per key id. On startup web and worker
  insert the row of each current key they hold if it is missing and compare it, so a key
  swapped under an unchanged id refuses to start. A retired file with no row, or a different
  value, is never loaded (``KeyUnavailable``). Negative results are not cached: restoring the
  file fixes it. A retired KEK comes out of the ring as a ``RetiredKek``, which only opens.
- A ring is **unverified** until that check passed (``KeyRing.verify``); an unverified ring
  refuses every use. Only a database unreachable at connect defers the check at startup.
- **The current KEK is required**; the pepper and the seed are optional in M0 (validated when
  present; M1 makes them required). **Web never holds the seed**: its ring has no seed, refuses
  any request for one (``SeedRefused``), and web refuses to start when a seed file is visible
  to it at all. The worker loads the seed and the pepper.
- Two kinds sharing one key is refused at startup.

Key material is never logged and never in an exception message.
"""

import asyncio
import hmac
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, NoReturn

from sqlalchemy import Column, DateTime, LargeBinary, MetaData, Table, Text, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings, secrets_dir
from ownvitals.crypto.envelope import (
    Kek,
    KeyKind,
    RetiredKek,
    RingKey,
    WebhookPepper,
    WebhookSeed,
    check_value,
    is_key_id,
)
from ownvitals.db import system_session
from ownvitals.db.role_check import is_authentication_failure

logger = logging.getLogger(__name__)

Role = Literal["web", "worker"]
ROLES: tuple[Role, ...] = ("web", "worker")
# The current file of each kind; a retired key of that kind is ``<file>_<id>``.
CURRENT_FILES = {
    KeyKind.KEK: "connection_kek",
    KeyKind.SEED: "webhook_seed",
    KeyKind.PEPPER: "webhook_pepper",
}
SETTINGS = {
    KeyKind.KEK: "connection_kek_id",
    KeyKind.SEED: "webhook_seed_id",
    KeyKind.PEPPER: "webhook_pepper_id",
}
# The migration that creates the check table, named when it is missing.
MIGRATION = "0004_connection_keks"
UNDEFINED_TABLE = "42P01"
_FILE_CONTENT = re.compile(rb"[0-9a-f]{64}\n?")
_MAX_FILE_BYTES = 66  # more than a valid file can hold, so a longer one fails the pattern


metadata = MetaData()
connection_keks = Table(
    "connection_keks",
    metadata,
    Column("key_id", Text, primary_key=True),
    Column("check_value", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


class KeyRingError(RuntimeError):
    """Web and worker refuse to start: a current key is missing, malformed or swapped, the
    check table is missing, or web can see the webhook seed."""


class KeyUnavailable(Exception):
    """The key a row names is missing, malformed or does not match its check row."""

    def __init__(self) -> None:
        super().__init__("key_unavailable")


class SeedRefused(Exception):
    """Web asked its ring for the webhook seed, which web never holds (ADR 0004 section 4)."""

    def __init__(self) -> None:
        super().__init__("seed_refused")


def _read_key(path: Path) -> bytes | None:
    """The 32 key bytes of ``path``; ``None`` when there is no such file; ``ValueError``
    otherwise (never saying what the file holds)."""
    try:
        with path.open("rb") as file:
            raw = file.read(_MAX_FILE_BYTES)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("unreadable") from None
    if _FILE_CONTENT.fullmatch(raw) is None:
        raise ValueError("malformed")
    return bytes.fromhex(raw[:64].decode("ascii"))


def _load(path: Path) -> bytes | None:
    """``_read_key`` for a retired key: a malformed or unreadable file counts as absent."""
    try:
        return _read_key(path)
    except ValueError:
        return None


def read_current[K: RingKey](cls: type[K], key_id: str, directory: Path) -> K | None:
    """The current key of ``cls``'s kind from its file, ``None`` when the file is absent, or
    ``KeyRingError`` (a bad id, or a file that is not 64 lowercase hex characters)."""
    kind = cls.KIND
    name = CURRENT_FILES[kind]
    if not is_key_id(key_id, kind):
        raise KeyRingError(f"{SETTINGS[kind]} does not match ^{kind.id_pattern.pattern}$")
    try:
        key = _read_key(directory / name)
    except ValueError:
        key, malformed = None, True
    else:
        malformed = False
    if malformed:
        raise KeyRingError(
            f"the {name} secret file must hold exactly 64 lowercase hexadecimal characters, "
            "optionally followed by one newline"
        )
    return None if key is None else cls(key_id, key)


def read_current_kek(key_id: str, directory: Path) -> Kek:
    """The current KEK, which web and worker can't start without, or ``KeyRingError``."""
    kek = read_current(Kek, key_id, directory)
    if kek is None:
        raise KeyRingError(
            "the connection_kek secret file is missing (scripts/worktree.sh init creates it for "
            "the local stack; see docs/dev/crypto.md)"
        )
    return kek


def _refuse_seed_files(directory: Path) -> None:
    """Web refuses to start when it can see any seed file, current or retired."""
    prefix = CURRENT_FILES[KeyKind.SEED]
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError:
        raise KeyRingError(
            "the secrets directory cannot be listed, so web cannot check that it holds no "
            "webhook seed"
        ) from None
    if any(name == prefix or name.startswith(prefix + "_") for name in names):
        raise KeyRingError(
            "web must never hold the webhook seed (ADR 0004 section 4): remove webhook_seed "
            "from the web service's secrets"
        )


def _refuse_shared_keys(keys: list[RingKey]) -> None:
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            if hmac.compare_digest(first.key, second.key):
                raise KeyRingError(
                    f"{CURRENT_FILES[first.KIND]} and {CURRENT_FILES[second.KIND]} hold the "
                    "same key; each kind needs its own"
                )


async def stored_check_value(db: AsyncSession, key_id: str) -> bytes | None:
    value = (
        await db.execute(
            select(connection_keks.c.check_value).where(connection_keks.c.key_id == key_id)
        )
    ).scalar_one_or_none()
    return None if value is None else bytes(value)


async def _record_and_compare(db: AsyncSession, keys: Iterable[RingKey]) -> None:
    """Record each key's check value if its id has no row yet (whoever comes first), read the
    row back and compare. ``KeyRingError`` on a mismatch: a different key under an unchanged
    id."""
    for key in keys:
        expected = check_value(key)
        await db.execute(
            pg_insert(connection_keks)
            .values(key_id=key.key_id, check_value=expected)
            .on_conflict_do_nothing(index_elements=[connection_keks.c.key_id])
        )
        stored = await stored_check_value(db, key.key_id)
        if stored is None or not hmac.compare_digest(stored, expected):
            raise KeyRingError(
                f"the {CURRENT_FILES[key.KIND]} file does not match the check value recorded "
                "for its id (a different key under an unchanged id); refusing to start"
            )


async def _check_rows(factory: async_sessionmaker[AsyncSession], keys: list[RingKey]) -> bool:
    """The check of ``keys`` in a ``system_session`` of its own, committed before it returns
    ``True``. ``False`` only when the database is unreachable at connect (connection refused, a
    connect timeout, DNS: an ``OperationalError`` before the session holds a connection that is
    not a refused login, the classification of ``db.role_check``). Anything else raises: a
    missing check table is ``KeyRingError`` naming the migration, and a refused login, a
    statement or lock timeout or any other error during the check propagates."""
    connected = missing_table = False
    try:
        async with system_session(factory) as db:
            connected = True
            await _record_and_compare(db, keys)
    except OperationalError as exc:
        if connected or is_authentication_failure(exc):
            raise
        return False
    except ProgrammingError as exc:
        if getattr(exc.orig, "sqlstate", None) != UNDEFINED_TABLE:
            raise
        missing_table = True
    if missing_table:  # raised here, so the error carries no SQL statement as its context
        raise KeyRingError(
            f"the connection_keks table is missing: run the migrations (up to {MIGRATION}) "
            "before starting web or the worker"
        )
    return True


class KeyRing:
    """The current keys, and retired ones loaded on demand. ``repr`` shows ids only.

    A ring starts **unverified**, however it was built, and only a check of its current keys
    against their check rows (``verify``) marks it verified. ``load_keyring`` runs that check at
    startup. When the database was unreachable then, every use runs it first, and refuses until
    it passes (``KeyRingError``); a mismatch refuses every use, the same as a refused start. The
    check runs in its own ``system_session`` (a second pool connection while the caller holds
    its own), one at a time, so a caller's transaction that rolls back afterwards can never leave
    the ring verified while the check rows were never committed.
    """

    def __init__(
        self,
        role: Role,
        directory: Path,
        kek: Kek,
        *,
        factory: async_sessionmaker[AsyncSession],
        pepper: WebhookPepper | None = None,
        seed: WebhookSeed | None = None,
    ) -> None:
        if role not in ROLES:
            raise ValueError("the role is web or worker")
        if role == "web" and seed is not None:
            raise SeedRefused
        if type(kek) is not Kek:
            raise TypeError("the current KEK is a Kek")
        self._role: Role = role
        self._directory = directory
        self._kek = kek
        self._pepper = pepper
        self._seed = seed
        self._factory = factory
        self._verified = False
        self._checking = asyncio.Lock()
        self._retired: dict[tuple[KeyKind, str], RingKey] = {}

    @property
    def role(self) -> Role:
        return self._role

    @property
    def current_id(self) -> str:
        """The current KEK's id."""
        return self._kek.key_id

    @property
    def pepper_id(self) -> str | None:
        """The current pepper's id, ``None`` when no pepper file is installed (M0)."""
        return None if self._pepper is None else self._pepper.key_id

    @property
    def seed_id(self) -> str | None:
        """The current seed's id, ``None`` in web and when no seed file is installed (M0)."""
        return None if self._seed is None else self._seed.key_id

    @property
    def verified(self) -> bool:
        return self._verified

    def __repr__(self) -> str:
        return (
            f"KeyRing(role={self._role!r}, kek={self.current_id!r}, "
            f"pepper={self.pepper_id!r}, seed={self.seed_id!r})"
        )

    def __reduce__(self) -> NoReturn:
        raise TypeError("a KeyRing cannot be pickled or copied")

    def _current_keys(self) -> list[RingKey]:
        return [key for key in (self._kek, self._pepper, self._seed) if key is not None]

    async def _try_verify(self) -> bool:
        """Run the check unless it passed already: ``False`` while the database is
        unreachable at connect; ``KeyRingError`` (or the database error) when it fails."""
        async with self._checking:
            if not self._verified and await _check_rows(self._factory, self._current_keys()):
                self._verified = True  # only after the check rows' transaction committed
        return self._verified

    async def verify(self) -> None:
        """Check the current keys against their rows unless that passed already, or refuse:
        ``KeyRingError`` (an unreachable database, a missing table, a mismatch) or the error
        that stopped the check. ``/api/ready`` and every use call it."""
        if not await self._try_verify():
            raise KeyRingError(
                "the key ring is not verified yet: the database was unreachable for its check"
            )

    async def current(self, db: AsyncSession) -> Kek:
        """The current KEK, the only one that writes (``db`` is not used for the check)."""
        await self.verify()
        return self._kek

    async def kek_for(self, db: AsyncSession, key_id: str) -> Kek | RetiredKek:
        """The KEK named ``key_id`` (a row's column): the current ``Kek``, or a ``RetiredKek``
        that only opens (ADR 0004 section 3); ``KeyUnavailable`` when there is none."""
        if key_id == self._kek.key_id:
            return await self.current(db)
        return await self._retired_key(db, RetiredKek, key_id)

    async def current_pepper(self, db: AsyncSession) -> WebhookPepper:
        """The current pepper, or ``KeyUnavailable`` when none is installed."""
        await self.verify()
        if self._pepper is None:
            raise KeyUnavailable
        return self._pepper

    async def pepper_for(self, db: AsyncSession, pepper_id: str) -> WebhookPepper:
        """The pepper named ``pepper_id`` (an entry's ``pepper_id``), or ``KeyUnavailable``."""
        if self._pepper is not None and pepper_id == self._pepper.key_id:
            return await self.current_pepper(db)
        return await self._retired_key(db, WebhookPepper, pepper_id)

    async def current_seed(self, db: AsyncSession) -> WebhookSeed:
        """The current seed (worker only), or ``KeyUnavailable`` when none is installed."""
        self._refuse_seed_in_web()
        await self.verify()
        if self._seed is None:
            raise KeyUnavailable
        return self._seed

    async def seed_for(self, db: AsyncSession, seed_id: str) -> WebhookSeed:
        """The seed named ``seed_id`` (worker only), or ``KeyUnavailable``."""
        self._refuse_seed_in_web()
        if self._seed is not None and seed_id == self._seed.key_id:
            return await self.current_seed(db)
        return await self._retired_key(db, WebhookSeed, seed_id)

    def _refuse_seed_in_web(self) -> None:
        if self._role != "worker":
            raise SeedRefused

    async def _retired_key[K: RingKey](self, db: AsyncSession, cls: type[K], key_id: str) -> K:
        await self.verify()  # an unverified ring refuses every use, retired keys included
        kind = cls.KIND
        if not is_key_id(key_id, kind):
            raise KeyUnavailable
        cached = self._retired.get((kind, key_id))
        if isinstance(cached, cls):
            return cached
        key = _load(self._directory / f"{CURRENT_FILES[kind]}_{key_id}")
        if key is None:
            raise KeyUnavailable
        loaded = cls(key_id, key)
        stored = await stored_check_value(db, key_id)
        if stored is None or not hmac.compare_digest(stored, check_value(loaded)):
            raise KeyUnavailable
        self._retired[(kind, key_id)] = loaded
        return loaded


async def load_keyring(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    role: Role,
    directory: Path | None = None,
) -> KeyRing:
    """Read the current keys of ``role`` and check their rows: ``KeyRingError`` when the KEK is
    missing or malformed, a present seed or pepper file is malformed, two kinds share a key, web
    can see a seed file, the check table is missing, or a check row doesn't match; any other
    error during the check (a statement or lock timeout among them) propagates. Only a database
    that is unreachable at connect is not an error here, as for the role check: the ring starts
    unverified, ``/api/ready`` answers 503 and every use refuses until the check passes."""
    if role not in ROLES:
        raise ValueError("the role is web or worker")
    directory = directory if directory is not None else secrets_dir()
    kek = read_current_kek(settings.connection_kek_id, directory)
    pepper = read_current(WebhookPepper, settings.webhook_pepper_id, directory)
    seed: WebhookSeed | None = None
    if role == "web":
        _refuse_seed_files(directory)
    else:
        seed = read_current(WebhookSeed, settings.webhook_seed_id, directory)
    _refuse_shared_keys([key for key in (kek, pepper, seed) if key is not None])
    ring = KeyRing(role, directory, kek, factory=factory, pepper=pepper, seed=seed)
    if not await ring._try_verify():
        logger.debug("database unreachable at startup; the key ring is checked before use")
    return ring
