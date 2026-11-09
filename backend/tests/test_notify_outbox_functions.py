"""The outbox's definer family (ADR 0007 section 3, #194): ``mail_outbox_claim``,
``mail_outbox_mark`` and ``owner_notice_queue``, their role, and the startup check that pins them.
Synthetic users (``example.test``) only; no health data.

- each function is executable by the app role and by no other caller role
  (``test_each_function_is_the_app_role_s_only``);
- none takes an owner id; the owner-notice write takes only a kind and its route comes from the
  fixed mapping (``test_the_signatures_take_no_owner_and_no_route``,
  ``test_an_owner_notice_takes_its_kind_s_route``);
- at most one pending owner notice per kind (``test_a_second_pending_owner_notice_is_coalesced``);
- the claim returns the minimum and the mark takes an id and an outcome; retries, leases,
  retention and concurrent claims (the ``test_the_claim_*`` and ``test_the_mark_*`` tests);
- the role has exactly its attributes and grants (guard 2, ``role_guard``) and the bodies their
  shipped digests; a drifted function is refused by guard 9 and by the startup check of web and
  the worker, and running the script again repairs it (``test_drift_*``).
"""

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from dbtools import OUTBOX_SCRIPT
from jobcore import settings_for
from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    admin_url,
    engine_for,
    ensure_user,
    queue_schema_fixture,
    run_script,
)
from ownvitals.db import DatabaseRoleError
from ownvitals.db.outbox_digests import OUTBOX_DIGESTS
from ownvitals.db.queue_check import (
    OUTBOX_FUNCTION_NAMES,
    OUTBOX_PINS,
    QUEUE_QUERY,
    queue_problems,
)
from ownvitals.notify.outbox import OWNER_NOTICE_KINDS, OWNER_NOTICE_ROUTES
from ownvitals.worker.loop import create_worker_engine, verify_startup
from queue_guard import check_queue
from role_guard import check_roles

BACKEND = Path(__file__).resolve().parent.parent
OUTBOX = "ownvitals_mail_outbox"
REFUSED = "42501"
INVALID = "22023"
UNDEFINED = "42883"
CLAIM = "SELECT id, kind, route, owner_id FROM mail_outbox_claim(:limit, :lease)"
MARK = "SELECT mail_outbox_mark(:id, :outcome)"
NOTICE = "SELECT owner_notice_queue(:kind)"
DRIFTED = "job queue does not match ADR 0005"
CALLS = {
    "mail_outbox_claim": ("SELECT * FROM mail_outbox_claim(1, 60)", {}),
    "mail_outbox_mark": ("SELECT mail_outbox_mark(:id, 'sent')", {"id": uuid.uuid4()}),
    "owner_notice_queue": ("SELECT owner_notice_queue('connection_needs_reconnect')", {}),
}


@pytest.fixture(name="outbox")
def outbox_fixture(admin: Connection) -> Connection:
    """The superuser connection on empty outbox tables (and empty job tables, ``admin``)."""
    admin.execute(text("TRUNCATE mail_outbox, owner_notices"))
    return admin


@pytest.fixture(name="reinstall")
def reinstall_fixture(queue_schema: str) -> Iterator[str]:
    """The admin URL; 50-outbox.sql runs again afterwards, whatever a test planted."""
    yield queue_schema
    run_script(queue_schema, OUTBOX_SCRIPT.read_text(encoding="utf-8"))


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _user(su: Connection) -> uuid.UUID:
    user = uuid.uuid4()
    ensure_user(su, user)
    return user


def _owned(su: Connection, owner: uuid.UUID, kind: str = "findings_waiting") -> uuid.UUID:
    route = "findings" if kind == "findings_waiting" else "connections"
    return su.execute(
        text("INSERT INTO mail_outbox (owner_id, kind, route) VALUES (:o, :k, :r) RETURNING id"),
        {"o": owner, "k": kind, "r": route},
    ).scalar_one()


def _claim(conn: Connection, limit: int = 10, lease: int = 60) -> list[tuple[object, ...]]:
    return [tuple(r) for r in conn.execute(text(CLAIM), {"limit": limit, "lease": lease}).all()]


def _state(su: Connection, table: str, row_id: object) -> tuple[object, ...]:
    return tuple(
        su.execute(
            text(f"SELECT state, attempts, hold_until IS NOT NULL FROM {table} WHERE id = :i"),  # noqa: S608
            {"i": row_id},
        ).one()
    )


# --- who may call ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", OUTBOX_FUNCTION_NAMES)
def test_each_function_is_the_app_role_s_only(
    outbox: Connection, app_database_url: str, name: str
) -> None:
    """The app role (web and the worker) may call it; the owner, maint, the other definer roles
    and a role with only PUBLIC's rights may not."""
    sql, params = CALLS[name]
    with engine_for(app_database_url) as engine, engine.begin() as connection:
        connection.execute(text(sql), params)
    outbox.execute(text("DROP ROLE IF EXISTS ownvitals_test_public"))
    outbox.execute(text("CREATE ROLE ownvitals_test_public NOLOGIN"))
    try:
        for role in (
            "ownvitals_owner",
            "ownvitals_maint",
            "ownvitals_queue",
            "ownvitals_webhook_events",
            "ownvitals_webhook_secrets",
            "ownvitals_metrics",
            "ownvitals_test_public",
        ):
            with engine_for(admin_url()) as engine, engine.connect() as connection:
                connection.execute(text(f"SET ROLE {role}"))
                with pytest.raises(DBAPIError) as raised:
                    connection.execute(text(sql), params)
                # PUBLIC has no USAGE on public, so the function is not even found there.
                allowed = {REFUSED, UNDEFINED} if role == "ownvitals_test_public" else {REFUSED}
                assert _sqlstate(raised.value) in allowed, role
    finally:
        outbox.execute(text("DROP ROLE ownvitals_test_public"))


def test_the_signatures_take_no_owner_and_no_route(outbox: Connection) -> None:
    rows = outbox.execute(
        text(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid), "
            "pg_get_function_result(p.oid) FROM pg_proc p WHERE p.proname = ANY(:n)"
        ),
        {"n": list(OUTBOX_FUNCTION_NAMES)},
    ).all()
    found = {name: (args, result) for name, args, result in rows}
    assert found == {
        "mail_outbox_claim": (
            "p_limit integer, p_lease_seconds integer",
            "TABLE(id uuid, kind text, route text, owner_id uuid)",
        ),
        "mail_outbox_mark": ("p_id uuid, p_outcome text", "boolean"),
        "owner_notice_queue": ("p_kind text", "boolean"),
    }
    assert {name: pin[0] for name, pin in OUTBOX_PINS.items()} == {
        name: args for name, (args, _) in found.items()
    }
    with pytest.raises(DBAPIError, match="does not exist"):  # no way to pass a route
        outbox.execute(text("SELECT owner_notice_queue('connection_needs_reconnect', 'admin')"))


# --- the owner-notice write --------------------------------------------------------------------


@pytest.mark.parametrize("kind", OWNER_NOTICE_KINDS)
def test_an_owner_notice_takes_its_kind_s_route(
    outbox: Connection, app_database_url: str, kind: str
) -> None:
    with engine_for(app_database_url) as engine, engine.begin() as connection:
        assert connection.execute(text(NOTICE), {"kind": kind}).scalar_one() is True
    rows = outbox.execute(text("SELECT kind, route, state FROM owner_notices")).all()
    assert [tuple(r) for r in rows] == [(kind, OWNER_NOTICE_ROUTES[kind], "pending")]


@pytest.mark.parametrize(
    "kind",
    ["findings_waiting", "connection_attention", "admin", "", "CONNECTION_NEEDS_RECONNECT", None],
)
def test_an_unknown_owner_notice_kind_is_refused(outbox: Connection, kind: str | None) -> None:
    with pytest.raises(DBAPIError) as raised:
        outbox.execute(text(NOTICE), {"kind": kind})
    assert _sqlstate(raised.value) == INVALID
    assert outbox.execute(text("SELECT count(*) FROM owner_notices")).scalar_one() == 0


def test_a_second_pending_owner_notice_is_coalesced(outbox: Connection) -> None:
    """#83 finding 1: at most one pending row per kind, also while one is being sent; once it
    was sent or failed, the next notice is a new row."""
    kind = {"kind": "connection_needs_reconnect"}
    assert outbox.execute(text(NOTICE), kind).scalar_one() is True
    assert outbox.execute(text(NOTICE), kind).scalar_one() is False
    assert outbox.execute(text("SELECT count(*) FROM owner_notices")).scalar_one() == 1
    [(first, *_)] = _claim(outbox)
    assert outbox.execute(text(NOTICE), kind).scalar_one() is False  # claimed: still coalesced
    outbox.execute(text(MARK), {"id": first, "outcome": "sent"})
    assert outbox.execute(text(NOTICE), kind).scalar_one() is True
    states = outbox.execute(text("SELECT state FROM owner_notices ORDER BY created_at")).scalars()
    assert list(states) == ["sent", "pending"]


# --- the claim ---------------------------------------------------------------------------------


def test_the_claim_returns_the_minimum_owner_notices_first(outbox: Connection) -> None:
    alice = _user(outbox)
    owned = _owned(outbox, alice)
    outbox.execute(text(NOTICE), {"kind": "connection_needs_reconnect"})
    notice = outbox.execute(text("SELECT id FROM owner_notices")).scalar_one()
    claimed = _claim(outbox)
    assert claimed == [
        (notice, "connection_needs_reconnect", "admin", None),
        (owned, "findings_waiting", "findings", alice),
    ]
    assert _state(outbox, "mail_outbox", owned) == ("claimed", 1, True)
    assert _claim(outbox) == []  # leased: not claimed again


def test_the_claim_takes_at_most_the_limit(outbox: Connection) -> None:
    for _ in range(3):
        _owned(outbox, _user(outbox))
    outbox.execute(text(NOTICE), {"kind": "connection_needs_reconnect"})
    assert len(_claim(outbox, limit=2)) == 2
    assert len(_claim(outbox, limit=1)) == 1
    assert len(_claim(outbox, limit=5)) == 1
    assert _claim(outbox) == []


@pytest.mark.parametrize(
    ("limit", "lease"), [(0, 60), (101, 60), (None, 60), (1, 29), (1, 601), (1, None)]
)
def test_the_claim_refuses_bounds_outside_its_own(
    outbox: Connection, limit: int | None, lease: int | None
) -> None:
    with pytest.raises(DBAPIError) as raised:
        _claim(outbox, limit, lease)  # type: ignore[arg-type]
    assert _sqlstate(raised.value) == INVALID


def test_the_claim_takes_back_an_expired_lease_and_fails_it_after_five(outbox: Connection) -> None:
    """A row claimed but never marked (a sender that died) is claimed again once its lease ends;
    after five attempts it is failed, never claimed a sixth time."""
    row = _owned(outbox, _user(outbox))
    for attempt in range(1, 6):
        assert [r[0] for r in _claim(outbox)] == [row]
        assert _state(outbox, "mail_outbox", row) == ("claimed", attempt, True)
        outbox.execute(
            text("UPDATE mail_outbox SET hold_until = now() - interval '1 second' WHERE id = :i"),
            {"i": row},
        )
    assert _claim(outbox) == []
    assert _state(outbox, "mail_outbox", row) == ("failed", 5, False)


def test_two_senders_never_claim_the_same_row(outbox: Connection) -> None:
    rows = {_owned(outbox, _user(outbox)) for _ in range(4)}
    with engine_for(admin_url()) as engine, engine.connect() as first, engine.connect() as second:
        mine = {r[0] for r in _claim(first, limit=2)}  # its transaction stays open: rows locked
        # Without SKIP LOCKED the second claim would wait on those rows: fail fast instead.
        second.execute(text("SET lock_timeout = '5s'"))
        theirs = {r[0] for r in _claim(second, limit=10)}
        second.commit()
        first.commit()
    assert len(mine) == 2
    assert mine | theirs == rows
    assert not mine & theirs


def test_the_claim_deletes_rows_sent_or_failed_thirty_days_ago(outbox: Connection) -> None:
    old_sent, old_failed, recent, pending = (_owned(outbox, _user(outbox)) for _ in range(4))
    outbox.execute(
        text(
            "UPDATE mail_outbox SET state = CASE id WHEN :s THEN 'sent' WHEN :f THEN 'failed' "
            "WHEN :r THEN 'sent' ELSE 'pending' END, updated_at = CASE WHEN id IN (:s, :f) "
            "THEN now() - interval '31 days' ELSE now() - interval '29 days' END"
        ),
        {"s": old_sent, "f": old_failed, "r": recent},
    )
    outbox.execute(
        text(
            "INSERT INTO owner_notices (kind, route, state, updated_at) "
            "VALUES ('connection_needs_reconnect', 'admin', 'sent', now() - interval '31 days')"
        )
    )
    assert [r[0] for r in _claim(outbox)] == [pending]
    left = outbox.execute(text("SELECT id FROM mail_outbox")).scalars()
    assert set(left) == {recent, pending}
    assert outbox.execute(text("SELECT count(*) FROM owner_notices")).scalar_one() == 0


# --- the mark ----------------------------------------------------------------------------------


def test_the_mark_changes_only_a_claimed_row(outbox: Connection) -> None:
    row = _owned(outbox, _user(outbox))
    assert outbox.execute(text(MARK), {"id": row, "outcome": "sent"}).scalar_one() is False
    assert _state(outbox, "mail_outbox", row) == ("pending", 0, False)
    _claim(outbox)
    assert outbox.execute(text(MARK), {"id": row, "outcome": "sent"}).scalar_one() is True
    assert _state(outbox, "mail_outbox", row) == ("sent", 1, False)
    assert outbox.execute(text(MARK), {"id": row, "outcome": "retry"}).scalar_one() is False
    unknown = {"id": uuid.uuid4(), "outcome": "sent"}
    assert outbox.execute(text(MARK), unknown).scalar_one() is False


def test_the_mark_retries_after_a_hold_and_fails_when_attempts_are_spent(
    outbox: Connection,
) -> None:
    row = _owned(outbox, _user(outbox))
    _claim(outbox)
    assert outbox.execute(text(MARK), {"id": row, "outcome": "retry"}).scalar_one() is True
    assert _state(outbox, "mail_outbox", row) == ("pending", 1, True)
    assert _claim(outbox) == []  # held for 2 minutes
    outbox.execute(text("UPDATE mail_outbox SET hold_until = now() WHERE id = :i"), {"i": row})
    assert [r[0] for r in _claim(outbox)] == [row]
    outbox.execute(text("UPDATE mail_outbox SET attempts = 5 WHERE id = :i"), {"i": row})
    assert outbox.execute(text(MARK), {"id": row, "outcome": "retry"}).scalar_one() is True
    assert _state(outbox, "mail_outbox", row) == ("failed", 5, False)


def test_the_mark_fails_an_owner_notice_for_good(outbox: Connection) -> None:
    outbox.execute(text(NOTICE), {"kind": "connection_needs_reconnect"})
    [(row, *_)] = _claim(outbox)
    assert outbox.execute(text(MARK), {"id": row, "outcome": "failed"}).scalar_one() is True
    assert _state(outbox, "owner_notices", row) == ("failed", 1, False)
    assert _claim(outbox) == []


@pytest.mark.parametrize("outcome", ["", "SENT", "delivered", None])
def test_the_mark_refuses_an_unknown_outcome(outbox: Connection, outcome: str | None) -> None:
    with pytest.raises(DBAPIError) as raised:
        outbox.execute(text(MARK), {"id": uuid.uuid4(), "outcome": outcome})
    assert _sqlstate(raised.value) == INVALID


# --- pins, guards and drift --------------------------------------------------------------------


async def startup_problems(app_database_url: str) -> str:
    """The worker's startup check message ('' when it passes); web runs the same check."""
    engine = create_worker_engine(settings_for(app_database_url))
    try:
        await verify_startup(engine)
    except DatabaseRoleError as exc:
        return str(exc)
    finally:
        await engine.dispose()
    return ""


async def test_the_installed_family_passes_every_check(
    outbox: Connection, app_database_url: str
) -> None:
    assert check_roles(outbox) == []
    assert check_queue(outbox) == []
    assert queue_problems(outbox.execute(text(QUEUE_QUERY)).one()) == []
    assert await startup_problems(app_database_url) == ""
    digests = dict(
        outbox.execute(
            text(
                "SELECT proname, md5(pg_get_functiondef(oid)) FROM pg_proc WHERE proname = ANY(:n)"
            ),
            {"n": list(OUTBOX_FUNCTION_NAMES)},
        ).all()
    )
    assert digests == OUTBOX_DIGESTS


def test_the_committed_digests_are_current(queue_schema: str) -> None:
    result = subprocess.run(  # noqa: S603 - our interpreter and our script
        [sys.executable, str(BACKEND / "tests" / "outbox_digests.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=BACKEND,
    )
    assert result.returncode == 0, result.stdout[-2000:]


def test_the_script_is_plain_sql_idempotent_and_refuses_a_non_superuser(
    outbox: Connection, reinstall: str, owner_database_url: str
) -> None:
    script = OUTBOX_SCRIPT.read_text(encoding="utf-8")
    assert not [line for line in script.splitlines() if line.startswith("\\")]
    assert "BEGIN;" not in script
    assert "COMMIT;" not in script
    with pytest.raises(Exception, match="must run as the superuser"):
        run_script(owner_database_url, script)
    run_script(reinstall, script)
    run_script(reinstall, script)
    assert check_roles(outbox) == []
    assert check_queue(outbox) == []


F = "public.mail_outbox_claim(integer, integer)"
DRIFT = [
    (f"GRANT EXECUTE ON FUNCTION {F} TO PUBLIC", "mail_outbox_claim"),
    (f"GRANT EXECUTE ON FUNCTION {F} TO ownvitals_owner", "mail_outbox_claim"),
    (f"REVOKE EXECUTE ON FUNCTION {F} FROM ownvitals_app", "mail_outbox_claim"),
    (f"ALTER FUNCTION {F} SECURITY INVOKER", "mail_outbox_claim"),
    (f"ALTER FUNCTION {F} SET search_path = public, pg_temp", "mail_outbox_claim"),
    (f"COMMENT ON FUNCTION {F} IS 'ownvitals-outbox v0'", "mail_outbox_claim"),
    (f"ALTER FUNCTION {F} OWNER TO ownvitals_owner", "mail_outbox_claim"),
    (
        "CREATE OR REPLACE FUNCTION public.owner_notice_queue(p_kind text) RETURNS boolean"
        " LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
        " AS $$ BEGIN RETURN true; END $$",
        "owner_notice_queue",
    ),
]


@pytest.mark.parametrize(("statement", "subject"), DRIFT)
async def test_drift_is_found_refused_and_repaired(
    outbox: Connection, reinstall: str, app_database_url: str, statement: str, subject: str
) -> None:
    outbox.execute(text(statement))
    try:
        assert any(subject in problem for problem in check_queue(outbox))
        found = await startup_problems(app_database_url)
        assert DRIFTED in found
        assert subject in found
    finally:
        run_script(reinstall, OUTBOX_SCRIPT.read_text(encoding="utf-8"))
    assert check_queue(outbox) == []
    assert await startup_problems(app_database_url) == ""


ROLE_DRIFT = [
    f"ALTER ROLE {OUTBOX} LOGIN",
    f"ALTER ROLE {OUTBOX} NOBYPASSRLS",
    f"ALTER ROLE {OUTBOX} INHERIT",
    f"ALTER ROLE {OUTBOX} SET work_mem = '64MB'",
    f"GRANT pg_read_all_data TO {OUTBOX}",
    f"GRANT {OUTBOX} TO ownvitals_owner",
    f"GRANT SELECT ON public.users TO {OUTBOX}",
    f"GRANT SELECT (email) ON public.users TO {OUTBOX}",
    f"GRANT INSERT ON public.mail_outbox TO {OUTBOX}",
    f"GRANT UPDATE (kind) ON public.owner_notices TO {OUTBOX}",
    f"GRANT CREATE ON SCHEMA public TO {OUTBOX}",
    "GRANT SELECT ON public.owner_notices TO ownvitals_app",
    "GRANT INSERT (kind, route) ON public.owner_notices TO ownvitals_app",
]


@pytest.mark.parametrize("statement", ROLE_DRIFT)
def test_guard_2_finds_a_drifted_role_and_the_script_repairs_it(
    outbox: Connection, reinstall: str, statement: str
) -> None:
    outbox.execute(text(statement))
    try:
        assert check_roles(outbox) != []
    finally:
        run_script(reinstall, OUTBOX_SCRIPT.read_text(encoding="utf-8"))
    assert check_roles(outbox) == []
