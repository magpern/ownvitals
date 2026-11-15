"""The token generation compare-and-swap (ADR 0004 section 6). Synthetic tokens only."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals.connections import credential_rows
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.repository import connect, disconnect, open_credentials, write_tokens
from ownvitals.connections.tables import DISCONNECTED, connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    DecryptFailed,
    canonical_json,
    connection_aad,
    connection_wrap_aad,
    seal,
)
from ownvitals.db import user_session

ACCESS = "synthetic-access-token-aaaaaaaa"
REFRESH = "synthetic-refresh-token-bbbbbbb"
REPLACED = "synthetic-access-token-replaced1"


async def test_a_stale_generation_cannot_overwrite_tokens(env: Env) -> None:
    user_id = await env.user("generation@example.com")
    other_id = await env.user("generation-other@example.com")
    ctx = make_context(user_id)
    ring = env.app.state.keyring
    info = await connect(env.factory, ring, ctx, "test", ACCESS, REFRESH)
    assert info.token_generation == 1

    async with user_session(env.factory, ctx) as db:
        kek = await ring.current(db)
    sealed = seal(
        canonical_json({"access_token": REPLACED, "refresh_token": REFRESH}),
        connection_aad(user_id, "test", info.id),
        kek,
        connection_wrap_aad(kek.key_id, user_id, "test", info.id),
    )
    async with user_session(env.factory, make_context(other_id)) as db:
        stolen = await write_tokens(
            db, make_context(other_id), info.id, 1, sealed, public={}, expires_at=None
        )
    assert stolen is False
    async with user_session(env.factory, ctx) as db:
        stale = await write_tokens(db, ctx, info.id, 0, sealed, public={}, expires_at=None)
        fresh = await write_tokens(db, ctx, info.id, 1, sealed, public={}, expires_at=None)
    assert stale is False
    assert fresh is True
    opened = await open_credentials(env.factory, ring, ctx, info.id)
    assert opened.generation == 2
    assert opened.credentials.access_token == REPLACED
    assert ACCESS not in repr(opened.credentials)


async def test_a_token_write_cannot_bring_back_a_disconnected_row(env: Env) -> None:
    """A disconnect keeps the generation (ADR 0004 section 6), so the generation alone does not
    stop a writer that read the row before it; the status does. Only a reconnect may (#279)."""
    user_id = await env.user("generation-disconnected@example.com")
    ctx = make_context(user_id)
    ring = env.app.state.keyring
    info = await connect(env.factory, ring, ctx, "test", ACCESS, REFRESH)
    assert await disconnect(env.factory, ctx, "test")
    async with user_session(env.factory, ctx) as db:
        kek = await ring.current(db)
    sealed = seal(
        canonical_json({"access_token": REPLACED, "refresh_token": REFRESH}),
        connection_aad(user_id, "test", info.id),
        kek,
        connection_wrap_aad(kek.key_id, user_id, "test", info.id),
    )
    async with user_session(env.factory, ctx) as db:
        late = await write_tokens(db, ctx, info.id, 1, sealed, public={}, expires_at=None)
        status = (
            await db.execute(text("SELECT status FROM connections WHERE id = :i"), {"i": info.id})
        ).scalar_one()
    assert late is False
    assert status == "disconnected"
    async with user_session(env.factory, ctx) as db:
        again = await write_tokens(
            db, ctx, info.id, 1, sealed, public={}, expires_at=None, reconnect=True
        )
    assert again is True
    opened = await open_credentials(env.factory, ring, ctx, info.id)
    assert (opened.generation, opened.credentials.access_token) == (2, REPLACED)


# ------------------------------------------- a status change during a failed decrypt (#397)
# ``open_credentials`` flags a ciphertext that will not open as ``needs_reconnect``. A disconnect
# keeps the generation, so only the status guard stops that late flag from turning a row that is
# no longer connected into ``needs_reconnect``. The row is read ``FOR UPDATE``, so the change is
# made inside the same transaction, between the read and the flag. The GCM failure is injected;
# the tokens are synthetic.


class _RaceRing:
    """The real ring, but ``hook`` runs once, on the reading session, before the key is used."""

    def __init__(self, ring: object, hook: Callable[[AsyncSession], Awaitable[None]] | None):
        self._ring, self._hook = ring, hook

    async def kek_for(self, db: AsyncSession, key_id: object) -> object:
        if self._hook is not None:
            hook, self._hook = self._hook, None
            await hook(db)
        return await self._ring.kek_for(db, key_id)  # type: ignore[attr-defined]


def _fail_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> bytes:
        raise DecryptFailed

    monkeypatch.setattr(credential_rows, "open_sealed", refuse)


async def _state(env: Env, ctx: UserContext, connection_id: UUID) -> tuple[str, bool, str | None]:
    async with user_session(env.factory, ctx) as db:
        row = (
            await db.execute(
                text("SELECT status, secret IS NULL, last_error FROM connections WHERE id = :i"),
                {"i": connection_id},
            )
        ).one()
    return str(row[0]), bool(row[1]), row[2]


async def test_a_failed_decrypt_flags_a_connected_row(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(await env.user("decrypt-flag@example.com"))
    info = await connect(env.factory, env.app.state.keyring, ctx, "test", ACCESS, REFRESH)
    _fail_decrypt(monkeypatch)
    ring = _RaceRing(env.app.state.keyring, None)
    with pytest.raises(ConnectionFailure, match="decrypt_failed"):
        await open_credentials(env.factory, ring, ctx, info.id)  # type: ignore[arg-type]
    assert await _state(env, ctx, info.id) == ("needs_reconnect", False, "decrypt_failed")


async def test_a_disconnect_during_a_failed_decrypt_stays_disconnected(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(await env.user("decrypt-race@example.com"))
    info = await connect(env.factory, env.app.state.keyring, ctx, "test", ACCESS, REFRESH)
    _fail_decrypt(monkeypatch)

    async def hook(db: AsyncSession) -> None:
        # the disconnect's write (``repository.disconnect``): no token write, same generation
        await db.execute(
            update(connections)
            .where(connections.c.id == info.id)
            .values(
                status=DISCONNECTED,
                secret=None,
                secret_nonce=None,
                wrapped_dek=None,
                dek_nonce=None,
                key_id=None,
                last_error=None,
            )
        )

    ring = _RaceRing(env.app.state.keyring, hook)
    with pytest.raises(ConnectionFailure, match="decrypt_failed"):
        await open_credentials(env.factory, ring, ctx, info.id)  # type: ignore[arg-type]
    assert await _state(env, ctx, info.id) == ("disconnected", True, None)
    async with user_session(env.factory, ctx) as db:
        generation = (
            await db.execute(
                text("SELECT token_generation FROM connections WHERE id = :i"), {"i": info.id}
            )
        ).scalar_one()
    assert generation == info.token_generation  # only the status guard can have stopped it
