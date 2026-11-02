"""The token generation compare-and-swap (ADR 0004 section 6). Synthetic tokens only."""

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals.connections.repository import connect, open_credentials, write_tokens
from ownvitals.crypto.envelope import canonical_json, connection_aad, connection_wrap_aad, seal
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
