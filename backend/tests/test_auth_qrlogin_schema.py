"""Migration 0015_qr_login: ``qr_logins``'s checks, its one-live-row index and its ON DELETE
rules, and the QR columns of ``sessions`` (ADR 0011 section 5). The columns and grants are in
``test_auth_schema.py``. Synthetic rows only."""

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
                "SELECT id, 's', now() + interval '1 day' FROM users"
            )
        )
        connection.execute(
            text("INSERT INTO pre_sessions (token_hash, expires_at) VALUES (sha256('p'), now())")
        )
        connection.commit()
        yield connection
    engine.dispose()


ROW = (
    "INSERT INTO qr_logins (nonce_hash, presession_id, scan_deadline, desktop_browser, "
    "desktop_os{extra}) SELECT {nonce}, p.id, now(), 'Firefox', 'Windows'{values} "
    "FROM pre_sessions p, sessions s"
)


def row(nonce: str = "sha256('n')", **columns: str) -> str:
    extra = "".join(f", {c}" for c in columns)
    values = "".join(f", {v}" for v in columns.values())
    return ROW.format(nonce=nonce, extra=extra, values=values)


SCANNED = {
    "state": "'scanned'",
    "approver_user_id": "s.user_id",
    "approver_session_id": "s.id",
    "scanned_at": "now()",
    "approve_deadline": "now()",
}
APPROVED = {
    **SCANNED,
    "state": "'approved'",
    "approved_at": "now()",
    "approved_factor": "'totp'",
    "complete_deadline": "now()",
}

BAD = [
    row(nonce="'short'"),
    row(state="'other'"),
    row(desktop_place="''"),
    row(desktop_place="repeat('x', 65)"),
    row(desktop_ip="repeat('1', 65)"),
    # a pending row that names an approver
    row(approver_user_id="s.user_id"),
    # a scanned row without its approver or deadline
    row(state="'scanned'"),
    row(**{k: v for k, v in SCANNED.items() if k != "approve_deadline"}),
    # an approved row without its factor or deadline
    row(**{k: v for k, v in APPROVED.items() if k != "approved_factor"}),
    row(**{k: v for k, v in APPROVED.items() if k != "complete_deadline"}),
    row(**{**APPROVED, "approved_factor": "'passkey'"}),
    # sessions: the QR columns go together
    "UPDATE sessions SET origin = 'qr_approval'",
    "UPDATE sessions SET origin = 'other'",
    "UPDATE sessions SET approved_factor = 'totp', approved_by_session_id = id",
    "UPDATE sessions SET mfa_by_approval = true",
    "UPDATE sessions SET origin = 'qr_approval', approved_factor = 'sms', "
    "approved_by_session_id = id",
]


@pytest.mark.parametrize("statement", BAD)
def test_check_constraints(admin: Connection, statement: str) -> None:
    with pytest.raises(IntegrityError):
        admin.execute(text(statement))
    admin.rollback()


def test_the_good_rows_are_accepted(admin: Connection) -> None:
    admin.execute(text(row(desktop_place="repeat('x', 64)")))
    admin.execute(text(row(nonce="sha256('m')", **{**APPROVED, "state": "'completed'"})))
    admin.execute(text(row(nonce="sha256('d')", state="'denied'")))
    admin.execute(
        text(
            "UPDATE sessions SET origin = 'qr_approval', mfa_by_approval = true, "
            "approved_factor = 'recovery_code', approved_by_session_id = gen_random_uuid()"
        )
    )
    admin.rollback()


def test_one_live_row_per_pre_session(admin: Connection) -> None:
    admin.execute(text(row()))
    with pytest.raises(IntegrityError):
        admin.execute(text(row(nonce="sha256('m')", **SCANNED)))
    admin.rollback()
    admin.execute(text(row()))
    admin.execute(text("UPDATE qr_logins SET state = 'expired'"))
    admin.execute(text(row(nonce="sha256('m')")))  # an ended row does not count
    with pytest.raises(IntegrityError):
        admin.execute(text(row(nonce="sha256('m')", state="'expired'")))  # nonce unique
    admin.rollback()


def test_a_row_is_unbound_from_its_pre_session_and_session_and_goes_with_its_user(
    admin: Connection,
) -> None:
    admin.execute(text(row(**APPROVED)))
    admin.execute(text("DELETE FROM pre_sessions"))
    admin.execute(text("DELETE FROM sessions"))
    bound = admin.execute(
        text("SELECT presession_id, approver_session_id, state FROM qr_logins")
    ).one()
    assert tuple(bound) == (None, None, "approved")
    admin.execute(text("DELETE FROM users"))
    assert admin.execute(text("SELECT count(*) FROM qr_logins")).scalar_one() == 0
    admin.rollback()
