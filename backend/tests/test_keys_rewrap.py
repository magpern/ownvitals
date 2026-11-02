"""Rewrap is resumable across two KEKs (ADR 0004 section 3). Synthetic tokens only.

The first run moves one account. The command then moves the rest. Ciphertext and
``token_generation`` stay, and the authenticator moves with the connections.
"""

import argparse
import secrets
from io import StringIO
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from authtools import (  # noqa: F401
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    make_settings,
)
from ctx import make_context
from ownvitals.auth.tables import mfa_totp
from ownvitals.auth.totp_keys import open_totp_secret
from ownvitals.connections.repository import connect, open_credentials
from ownvitals.connections.rewrap import rewrap_owner
from ownvitals.connections.tables import connections
from ownvitals.crypto.keks import load_keyring
from ownvitals.db import user_session
from ownvitals.keys_cli import rewrap

ACCESS = "synthetic-access-token-aaaaaaaa"
REFRESH = "synthetic-refresh-token-bbbbbbb"


async def _stored(env: Env, user_id: UUID, connection_id: UUID) -> tuple[bytes, int, str]:
    async with user_session(env.factory, make_context(user_id)) as db:
        row = (
            await db.execute(
                select(
                    connections.c.secret,
                    connections.c.token_generation,
                    connections.c.key_id,
                    connections.c.wrapped_dek,
                ).where(connections.c.id == connection_id)
            )
        ).one()
    return bytes(row.secret), int(row.token_generation), str(row.key_id)


async def test_rewrap_is_resumable_across_two_keks(
    env: Env, app_database_url: str, secrets_dir: Path
) -> None:
    ring = env.app.state.keyring
    first_user = await env.user("rewrap-one@example.com")
    second_user = await env.user("rewrap-two@example.com")
    await env.enrol(first_user)
    first = await connect(env.factory, ring, make_context(first_user), "test", ACCESS, REFRESH)
    second = await connect(
        env.factory, ring, make_context(second_user), "test", ACCESS + "-two", REFRESH
    )
    before = {
        first_user: await _stored(env, first_user, first.id),
        second_user: await _stored(env, second_user, second.id),
    }
    assert {row[2] for row in before.values()} == {"kek-1"}

    current = secrets_dir / "connection_kek"
    (secrets_dir / "connection_kek_kek-1").write_bytes(current.read_bytes())
    current.write_bytes(secrets.token_bytes(32).hex().encode() + b"\n")
    settings = make_settings(app_database_url, connection_kek_id="kek-2")
    moved = await load_keyring(env.factory, settings, role="web")
    once = await rewrap_owner(env.factory, moved, make_context(first_user))
    assert once == {"kek-1 rewrapped": 1}
    still = await _stored(env, second_user, second.id)
    assert still[2] == "kek-1"
    assert still[0] == before[second_user][0]

    out, err = StringIO(), StringIO()
    code = await rewrap(argparse.Namespace(action="rewrap"), settings, out, err)
    assert code == 0, err.getvalue()
    printed = out.getvalue()
    assert "kek-2" in printed
    assert "kek-1 rewrapped: 1" in printed
    assert ACCESS not in printed
    assert str(first_user) not in printed
    assert str(second_user) not in printed
    assert err.getvalue() == ""

    for user_id, connection_id, token in (
        (first_user, first.id, ACCESS),
        (second_user, second.id, ACCESS + "-two"),
    ):
        secret, generation, key_id = await _stored(env, user_id, connection_id)
        assert (secret, generation, key_id) == (before[user_id][0], before[user_id][1], "kek-2")
        opened = await open_credentials(env.factory, moved, make_context(user_id), connection_id)
        assert opened.credentials.access_token == token
        assert token not in repr(opened.credentials)

    async with user_session(env.factory, make_context(first_user)) as db:
        totp_row = (
            await db.execute(select(mfa_totp).where(mfa_totp.c.user_id == first_user))
        ).one()
        seed = await open_totp_secret(db, moved, first_user, totp_row)
    assert totp_row.key_id == "kek-2"
    assert seed == TEST_TOTP_SECRET.encode()
