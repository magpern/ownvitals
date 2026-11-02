"""A connection failure carries its category and nothing else (ADR 0004 sections 8 and 9).

Synthetic tokens only. A ciphertext that will not open becomes ``needs_reconnect`` and the
log names the category and the row id.
"""

import logging
import pickle

import pytest
from sqlalchemy import select, text

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals.connections.credentials import Credentials
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.repository import connect, open_credentials
from ownvitals.connections.tables import connections
from ownvitals.db import user_session

ACCESS = "synthetic-access-token-aaaaaaaa"
REFRESH = "synthetic-refresh-token-bbbbbbb"


def test_a_failure_carries_only_its_category() -> None:
    failure = ConnectionFailure("decrypt_failed")
    assert failure.args == ("decrypt_failed",)
    assert failure.category == "decrypt_failed"
    assert failure.__cause__ is None
    assert failure.__context__ is None
    with pytest.raises(ValueError, match="unknown connection category"):
        ConnectionFailure("provider text")


def test_credentials_cannot_be_pickled_or_printed() -> None:
    credentials = Credentials("test", ACCESS, REFRESH)
    assert ACCESS not in repr(credentials)
    assert ACCESS not in str(credentials)
    with pytest.raises(TypeError):
        pickle.dumps(credentials)


async def test_a_corrupt_ciphertext_fails_closed_and_is_not_logged(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    user_id = await env.user("corrupt@example.com")
    ctx = make_context(user_id)
    info = await connect(env.factory, env.app.state.keyring, ctx, "test", ACCESS, REFRESH)
    async with user_session(env.factory, ctx) as db:
        await db.execute(
            text(
                "UPDATE connections SET secret = set_byte(secret, 0, get_byte(secret, 0) # 255) "
                "WHERE id = :id"
            ),
            {"id": info.id},
        )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConnectionFailure) as caught:
        await open_credentials(env.factory, env.app.state.keyring, ctx, info.id)
    failure = caught.value
    assert failure.args == ("decrypt_failed",)
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert ACCESS not in caplog.text
    assert REFRESH not in caplog.text
    logged = [record for record in caplog.records if record.getMessage() == "connection unusable"]
    found = [
        (getattr(record, "connection_id", None), getattr(record, "category", None))
        for record in logged
    ]
    assert found == [(str(info.id), "decrypt_failed")]
    async with user_session(env.factory, ctx) as db:
        status = (
            await db.execute(select(connections.c.status).where(connections.c.id == info.id))
        ).scalar_one()
    assert status == "needs_reconnect"
