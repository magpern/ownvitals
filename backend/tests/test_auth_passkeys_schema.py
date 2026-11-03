"""Migration 0013_passkeys: the passkey tables' checks, uniqueness and cascades (ADR 0011
sections 1, 2 and 4). The columns, grants and ON DELETE rules are in ``test_auth_schema.py``.
Synthetic rows only."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from authtools import auth_schema_fixture  # noqa: F401 - fixtures


@pytest.fixture
def admin(auth_schema: None, database_url: str):
    engine = create_engine(database_url)
    with engine.connect() as connection:
        connection.execute(text("TRUNCATE users, pre_sessions CASCADE"))
        connection.execute(
            text(
                "INSERT INTO users (email, email_normalized, email_verified_at) "
                "VALUES ('a@x.se', 'a@x.se', now())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions (user_id, token_hash, expires_at) "
                "SELECT id, 's', now() FROM users"
            )
        )
        connection.execute(
            text("INSERT INTO pre_sessions (token_hash, expires_at) VALUES (sha256('p'), now())")
        )
        connection.commit()
        yield connection
    engine.dispose()


CREDENTIAL = (
    "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, name{extra}) "
    "SELECT id, {cid}, 'k', {name}{values} FROM users"
)


def credential(cid: str = "'c'", name: str = "'Passkey'", **columns: str) -> str:
    extra = "".join(f", {c}" for c in columns)
    values = "".join(f", {v}" for v in columns.values())
    return CREDENTIAL.format(cid=cid, name=name, extra=extra, values=values)


BAD = [
    credential(cid="''"),
    credential(cid="decode(repeat('00', 1024), 'hex')"),
    credential(name="''"),
    credential(name="repeat('x', 65)"),
    credential(sign_count="-1"),
    credential(sign_count="4294967296"),
    credential(transports="array_fill('usb'::text, ARRAY[9])"),
    "UPDATE users SET webauthn_handle = 'short'",
    "INSERT INTO pre_sessions (token_hash, expires_at) VALUES ('short', now())",
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, expires_at) "
    "VALUES (sha256('x'), 'other', now())",
    # a sign-in challenge bound to a session, or to nothing
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, session_id, user_id, expires_at) "
    "SELECT sha256('x'), 'signin', id, user_id, now() FROM sessions",
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, expires_at) "
    "VALUES (sha256('x'), 'signin', now())",
    # a registration challenge without its session or user, or with a pre-session
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, user_id, expires_at) "
    "SELECT sha256('x'), 'register', id, now() FROM users",
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, session_id, expires_at) "
    "SELECT sha256('x'), 'reauth', id, now() FROM sessions",
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, session_id, user_id, "
    "presession_id, expires_at) SELECT sha256('x'), 'register', s.id, s.user_id, p.id, now() "
    "FROM sessions s, pre_sessions p",
    "INSERT INTO webauthn_challenges (challenge_hash, purpose, presession_id, expires_at) "
    "SELECT 'short', 'signin', id, now() FROM pre_sessions",
]


@pytest.mark.parametrize("statement", BAD)
def test_check_constraints(admin: Connection, statement: str) -> None:
    with pytest.raises(IntegrityError):
        admin.execute(text(statement))
    admin.rollback()


def test_the_good_rows_are_accepted(admin: Connection) -> None:
    admin.execute(text(credential(cid="decode(repeat('00', 1023), 'hex')", name="repeat('x', 64)")))
    admin.execute(text(credential(cid="'d'", sign_count="4294967295")))
    admin.execute(text("UPDATE users SET webauthn_handle = sha256('h')"))
    admin.execute(
        text(
            "INSERT INTO webauthn_challenges (challenge_hash, purpose, presession_id, expires_at) "
            "SELECT sha256('x'), 'signin', id, now() FROM pre_sessions"
        )
    )
    admin.rollback()


def test_uniqueness(admin: Connection) -> None:
    admin.execute(text(credential()))
    with pytest.raises(IntegrityError):
        admin.execute(text(credential()))  # one credential id, once
    admin.rollback()
    admin.execute(
        text(
            "INSERT INTO users (email, email_normalized, email_verified_at, webauthn_handle) "
            "VALUES ('b@x.se', 'b@x.se', now(), sha256('h'))"
        )
    )
    with pytest.raises(IntegrityError):
        admin.execute(text("UPDATE users SET webauthn_handle = sha256('h')"))
    admin.rollback()


def test_challenges_go_with_their_session_and_their_pre_session(admin: Connection) -> None:
    admin.execute(
        text(
            "INSERT INTO webauthn_challenges (challenge_hash, purpose, session_id, user_id, "
            "expires_at) SELECT sha256('r'), 'register', id, user_id, now() FROM sessions"
        )
    )
    admin.execute(
        text(
            "INSERT INTO webauthn_challenges (challenge_hash, purpose, presession_id, expires_at) "
            "SELECT sha256('s'), 'signin', id, now() FROM pre_sessions"
        )
    )
    admin.execute(text("DELETE FROM sessions"))
    admin.execute(text("DELETE FROM pre_sessions"))
    assert admin.execute(text("SELECT count(*) FROM webauthn_challenges")).scalar_one() == 0
    admin.rollback()
