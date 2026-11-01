"""Migration 0002_auth: the M1 system tables, their grants and their ON DELETE rules."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, ProgrammingError

from ownvitals.auth.tables import metadata
from ownvitals.db import SYSTEM_TABLES
from rls_guard import check_tables
from role_guard import check_roles

AUTH = sorted(metadata.tables)


@pytest.fixture
def admin(auth_schema: None, database_url: str):
    engine = create_engine(database_url)
    with engine.connect() as connection:
        connection.execute(text("TRUNCATE users CASCADE"))
        connection.commit()
        yield connection
    engine.dispose()


def test_there_are_twelve_auth_tables_all_listed_with_a_reason() -> None:
    assert len(AUTH) == 12
    for name in AUTH:
        assert SYSTEM_TABLES[name].strip(), name


def test_the_rls_and_role_guards_pass_with_the_auth_tables(admin: Connection) -> None:
    assert check_tables(admin, SYSTEM_TABLES) == []
    assert check_roles(admin) == []


def test_every_auth_table_is_a_system_table_without_owner_id_or_rls(admin: Connection) -> None:
    rows = admin.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, "
            "EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
            "AND a.attname = 'owner_id' AND NOT a.attisdropped) "
            "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
            "AND c.relname = ANY(:names)"
        ),
        {"names": AUTH},
    ).all()
    assert sorted(r[0] for r in rows) == AUTH
    assert not any(r[1] or r[2] for r in rows)


def test_the_migration_matches_the_table_definitions(admin: Connection) -> None:
    for name, table in metadata.tables.items():
        rows = admin.execute(
            text(
                "SELECT column_name, is_nullable = 'YES' FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        ).all()
        actual = {r[0]: r[1] for r in rows}
        expected = {c.name: c.nullable for c in table.columns}
        assert actual == expected, name


def test_the_app_role_gets_dml_and_nothing_else(admin: Connection) -> None:
    privileges = {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": True,
        "DELETE": True,
        "TRUNCATE": False,
        "REFERENCES": False,
        "TRIGGER": False,
    }
    for name in AUTH:
        for privilege, expected in privileges.items():
            granted = admin.execute(
                text("SELECT has_table_privilege('ownvitals_app', :t, :p)"),
                {"t": f"public.{name}", "p": privilege},
            ).scalar_one()
            assert granted is expected, (name, privilege)


def test_the_app_role_cannot_truncate_or_alter(app_database_url: str, auth_schema: None) -> None:
    engine = create_engine(app_database_url)
    try:
        with engine.connect() as connection:
            for statement in (
                "TRUNCATE users",
                "ALTER TABLE users ADD COLUMN x int",
                "DROP TABLE users",
            ):
                with pytest.raises(ProgrammingError):
                    connection.execute(text(statement))
                connection.rollback()
    finally:
        engine.dispose()


def test_on_delete_rules(admin: Connection) -> None:
    rows = admin.execute(
        text(
            "SELECT conrelid::regclass::text, a.attname, confrelid::regclass::text, confdeltype "
            "FROM pg_constraint c JOIN pg_attribute a "
            "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE contype = 'f' AND connamespace = 'public'::regnamespace "
            "AND conrelid::regclass::text = ANY(:tables)"
        ),
        {"tables": [*metadata.tables, "connections"]},
    ).all()
    rules = {(r[0], r[1]): (r[2], r[3]) for r in rows}
    set_null = {("invites", "created_by"), ("invites", "used_by"), ("auth_events", "user_id")}
    for (table, column), (target, action) in rules.items():
        expected = "n" if (table, column) in set_null else "c"
        assert action == expected, (table, column)
        assert target in {"users", "invites"}
    assert set_null <= set(rules)
    assert ("pending_signups", "invite_id") in rules
    assert ("oauth_flows", "invite_id") in rules
    assert rules[("connections", "owner_id")] == ("users", "c")  # ADR 0004 section 1
    assert len(rules) == 13


def test_deleting_a_user_cascades_except_events_and_invites(admin: Connection) -> None:
    user = admin.execute(
        text(
            "INSERT INTO users (email, email_normalized, email_verified_at) "
            "VALUES ('a@x.se', 'a@x.se', now()) RETURNING id"
        )
    ).scalar_one()
    for statement in (
        "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (:u, 's', now())",
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, 'github', '1')",
        "INSERT INTO known_devices (user_id, device_hash) VALUES (:u, 'd')",
        "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at) "
        "VALUES (:u, 'reset_password', 't', now())",
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id) "
        "VALUES (:u, 'c', 'n', 'app-v1')",
        "INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES (:u, 'r')",
        "INSERT INTO oauth_flows (flow_hash, provider, purpose, state_hash, pkce_verifier, "
        "user_id, expires_at) VALUES ('f', 'github', 'link', 's', 'v', :u, now())",
        "INSERT INTO auth_events (user_id, kind) VALUES (:u, 'sign_in')",
        "INSERT INTO invites (code_hash, created_by, used_by, expires_at) "
        "VALUES ('i', :u, :u, now())",
    ):
        admin.execute(text(statement), {"u": user})
    admin.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})
    for table in (
        "sessions", "identities", "known_devices", "auth_tokens", "mfa_totp",
        "mfa_recovery_codes", "oauth_flows",
    ):  # fmt: skip
        count = admin.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
        assert count == 0, table
    event = admin.execute(text("SELECT user_id FROM auth_events")).one()
    assert event[0] is None  # kept, with a null user until the 30-day purge
    invite = admin.execute(text("SELECT created_by, used_by FROM invites")).one()
    assert tuple(invite) == (None, None)


def test_deleting_an_invite_cascades_to_pending_signups_and_flows(admin: Connection) -> None:
    invite = admin.execute(
        text("INSERT INTO invites (code_hash, expires_at) VALUES ('i', now()) RETURNING id")
    ).scalar_one()
    admin.execute(
        text("INSERT INTO pending_signups (invite_id, expires_at) VALUES (:i, now())"),
        {"i": invite},
    )
    admin.execute(
        text(
            "INSERT INTO oauth_flows (flow_hash, provider, purpose, state_hash, pkce_verifier, "
            "invite_id, expires_at) VALUES ('f', 'google', 'sign_in', 's', 'v', :i, now())"
        ),
        {"i": invite},
    )
    admin.execute(text("DELETE FROM invites"))
    assert admin.execute(text("SELECT count(*) FROM pending_signups")).scalar_one() == 0
    assert admin.execute(text("SELECT count(*) FROM oauth_flows")).scalar_one() == 0


def test_uniqueness_rules(admin: Connection) -> None:
    def user(email: str) -> None:
        admin.execute(
            text(
                "INSERT INTO users (email, email_normalized, email_verified_at) "
                "VALUES (:e, :n, now())"
            ),
            {"e": email, "n": email.lower()},
        )

    user("A@x.se")
    with pytest.raises(IntegrityError):
        user("a@X.se")
    admin.rollback()
    admin.execute(text("TRUNCATE users CASCADE"))
    # Several pending sign-ups may share a normalized email: the first confirmation wins.
    for token in ("t1", "t2"):
        admin.execute(
            text(
                "INSERT INTO pending_signups (token_hash, email, email_normalized, expires_at) "
                "VALUES (:t, 'a@x.se', 'a@x.se', now())"
            ),
            {"t": token},
        )
    assert admin.execute(text("SELECT count(*) FROM pending_signups")).scalar_one() == 2
    admin.rollback()


def test_check_constraints(admin: Connection) -> None:
    bad = [
        "INSERT INTO users (email, email_normalized, email_verified_at, role) "
        "VALUES ('a', 'a', now(), 'root')",
        "INSERT INTO pending_signups (provider, expires_at) VALUES ('google', now())",
        "INSERT INTO pending_signups (email, expires_at) VALUES ('a@x.se', now())",
        "INSERT INTO oauth_flows (flow_hash, provider, purpose, state_hash, pkce_verifier, "
        "expires_at) VALUES ('f', 'myspace', 'sign_in', 's', 'v', now())",
    ]
    for statement in bad:
        with pytest.raises(IntegrityError):
            admin.execute(text(statement))
        admin.rollback()
