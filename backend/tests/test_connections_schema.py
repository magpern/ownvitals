"""The connections table (ADR 0004 section 1): credential columns and one live row.

Checks run as the superuser, which bypasses row-level security, so a rejected insert is the
constraint and not the policy. Rows are synthetic and rolled back.
"""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401

NONCE = b"n" * 12
WRAPPED = b"w" * 48
SECRET = b"ciphertext-not-a-token"


def _user(conn: Connection, email: str) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO users (email, email_normalized, email_verified_at) "
            "VALUES (:e, :e, now()) RETURNING id"
        ),
        {"e": email},
    ).scalar_one()


def _reject(conn: Connection, statement: str, params: dict[str, object]) -> None:
    nested = conn.begin_nested()
    with pytest.raises(DBAPIError):
        conn.execute(text(statement), params)
    nested.rollback()


def _insert(conn: Connection, user_id: UUID, **values: object) -> None:
    params: dict[str, object] = {
        "id": uuid4(),
        "owner_id": user_id,
        "provider": "test",
        "status": "connected",
        **values,
    }
    names = ", ".join(params)
    holders = ", ".join(f":{name}" for name in params)
    # Column names are the helper's own keys, not request data.
    conn.execute(
        text(f"INSERT INTO connections ({names}) VALUES ({holders})"),  # noqa: S608
        params,
    )


@pytest.fixture
def db(database_url: str, clean_auth: None) -> Iterator[Connection]:
    engine = create_engine(database_url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()
    engine.dispose()


def test_credentials_are_all_set_or_all_empty(db: Connection) -> None:
    user_id = _user(db, "schema-empty@example.com")
    _insert(db, user_id, status="disconnected")
    _insert(
        db,
        user_id,
        provider="withings",
        status="needs_reconnect",
        secret=SECRET,
        secret_nonce=NONCE,
        wrapped_dek=WRAPPED,
        dek_nonce=NONCE,
        key_id="kek-1",
    )
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status, secret) "
        "VALUES (:id, :owner, 'test', 'needs_reconnect', :secret)",
        {"id": uuid4(), "owner": user_id, "secret": SECRET},
    )
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status) "
        "VALUES (:id, :owner, 'test', 'connected')",
        {"id": uuid4(), "owner": user_id},
    )
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status, secret, secret_nonce, "
        "wrapped_dek, dek_nonce, key_id) VALUES (:id, :owner, 'test', 'disconnected', "
        ":secret, :nonce, :wrapped, :nonce, 'kek-1')",
        {"id": uuid4(), "owner": user_id, "secret": SECRET, "nonce": NONCE, "wrapped": WRAPPED},
    )


def test_a_credential_of_the_wrong_shape_is_refused(db: Connection) -> None:
    user_id = _user(db, "schema-shape@example.com")
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status, secret, secret_nonce, "
        "wrapped_dek, dek_nonce, key_id) VALUES (:id, :owner, 'test', 'connected', "
        ":secret, :nonce, :wrapped, :nonce, 'not-a-kek')",
        {"id": uuid4(), "owner": user_id, "secret": SECRET, "nonce": NONCE, "wrapped": WRAPPED},
    )
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status, secret, secret_nonce, "
        "wrapped_dek, dek_nonce, key_id) VALUES (:id, :owner, 'test', 'connected', "
        ":secret, :nonce, :wrapped, :dek, 'kek-1')",
        {
            "id": uuid4(),
            "owner": user_id,
            "secret": SECRET,
            "nonce": b"short",
            "wrapped": WRAPPED,
            "dek": NONCE,
        },
    )


def test_one_live_connection_per_user_and_provider(db: Connection) -> None:
    user_id = _user(db, "schema-live@example.com")
    other = _user(db, "schema-other@example.com")
    creds = {
        "secret": SECRET,
        "secret_nonce": NONCE,
        "wrapped_dek": WRAPPED,
        "dek_nonce": NONCE,
        "key_id": "kek-1",
    }
    _insert(db, user_id, status="disconnected")
    _insert(db, user_id, status="disconnected")
    _insert(db, user_id, **creds)
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status, secret, secret_nonce, "
        "wrapped_dek, dek_nonce, key_id) VALUES (:id, :owner, 'test', 'needs_reconnect', "
        ":secret, :nonce, :wrapped, :nonce, 'kek-1')",
        {"id": uuid4(), "owner": user_id, "secret": SECRET, "nonce": NONCE, "wrapped": WRAPPED},
    )
    _insert(db, user_id, provider="withings", **creds)
    _insert(db, other, **creds)
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status) "
        "VALUES (:id, :owner, 'spotify', 'disconnected')",
        {"id": uuid4(), "owner": user_id},
    )


def test_an_unknown_status_is_refused(db: Connection) -> None:
    user_id = _user(db, "schema-status@example.com")
    _reject(
        db,
        "INSERT INTO connections (id, owner_id, provider, status) "
        "VALUES (:id, :owner, 'test', 'blocked_by_provider_plan')",
        {"id": uuid4(), "owner": user_id},
    )
