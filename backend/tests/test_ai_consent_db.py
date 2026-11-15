"""Migration 0021's tables (ADR 0014 section 3; #206) as the superuser: the vocabularies of
``ownvitals.ai.tables`` equal the check constraints, one live consent per owner, and a message
only in a conversation of its own owner. Synthetic rows only."""

import re
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from jobtools import admin_url, engine_for, ensure_user
from ownvitals.ai.tables import AI_CONSENT_OWNED, MESSAGE_MAX_CHARS, MESSAGE_ROLES

SYNTHETIC = True
HASH = b"\x5a" * 32


@pytest.fixture(name="su")
def su_fixture(clean_auth: None) -> Iterator[Connection]:
    with engine_for(admin_url()) as engine, engine.connect() as conn:
        yield conn
        conn.rollback()


def _owner(su: Connection) -> UUID:
    owner = uuid4()
    ensure_user(su, owner)
    return owner


def _check(su: Connection, name: str) -> str:
    return su.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"), {"n": name}
    ).scalar_one()


def test_the_roles_are_the_check_constraint(su: Connection) -> None:
    found = re.findall(r"'([a-z_]+)'::text", _check(su, "ai_messages_role"))
    assert tuple(found) == MESSAGE_ROLES
    assert str(MESSAGE_MAX_CHARS) in _check(su, "ai_messages_content")
    tables = su.execute(
        text("SELECT relname FROM pg_class WHERE relname = ANY(:t) AND relforcerowsecurity"),
        {"t": list(AI_CONSENT_OWNED)},
    ).scalars()
    assert set(tables) == set(AI_CONSENT_OWNED)


def test_one_live_consent_per_owner(su: Connection) -> None:
    owner = _owner(su)
    insert = text(
        "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256, withdrawn_at) "
        "VALUES (:o, 1, 'en', :h, :w)"
    )
    su.execute(insert, {"o": owner, "h": HASH, "w": None})
    su.execute(insert, {"o": owner, "h": HASH, "w": "2999-01-01"})  # a withdrawn one beside it
    with pytest.raises(IntegrityError):
        su.execute(insert, {"o": owner, "h": HASH, "w": None})


@pytest.mark.parametrize(
    ("column", "value"),
    [("text_sha256", b"\x01" * 31), ("locale", "EN"), ("consent_version", 0)],
)
def test_the_consent_checks(su: Connection, column: str, value: object) -> None:
    owner = _owner(su)
    values = {"consent_version": 1, "locale": "en", "text_sha256": HASH, column: value}
    with pytest.raises(IntegrityError):
        su.execute(
            text(
                "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
                "VALUES (:o, :consent_version, :locale, :text_sha256)"
            ),
            {"o": owner, **values},
        )


def test_a_message_belongs_to_a_conversation_of_its_owner(su: Connection) -> None:
    a, b = _owner(su), _owner(su)
    conversation = su.execute(
        text("INSERT INTO ai_conversations (owner_id) VALUES (:o) RETURNING id"), {"o": a}
    ).scalar_one()
    insert = text(
        "INSERT INTO ai_messages (owner_id, conversation_id, role, content) VALUES (:o, :c, :r, :t)"
    )
    su.execute(insert, {"o": a, "c": conversation, "r": "user", "t": "synthetic"})
    for owner, role, content in (
        (b, "user", "synthetic"),  # another owner's conversation
        (a, "system", "synthetic"),  # not a role
        (a, "user", ""),  # empty
        (a, "user", "x" * (MESSAGE_MAX_CHARS + 1)),  # too long
    ):
        su.execute(text("SAVEPOINT s"))
        with pytest.raises(IntegrityError):
            su.execute(insert, {"o": owner, "c": conversation, "r": role, "t": content})
        su.execute(text("ROLLBACK TO SAVEPOINT s"))
    su.execute(text("DELETE FROM ai_conversations WHERE id = :c"), {"c": conversation})
    left = su.execute(text("SELECT count(*) FROM ai_messages WHERE owner_id = :o"), {"o": a})
    assert left.scalar_one() == 0  # the messages cascade with their conversation
