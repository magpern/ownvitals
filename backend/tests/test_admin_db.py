"""The audited maint path (``ownvitals.admin.db``): every transaction writes its audit row first;
a read is read only; a refusal or an error leaves one row of its own; the role is checked on
connect and on checkout. Synthetic accounts only."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from admintools import (
    admin_schema_fixture,  # noqa: F401 - fixtures
    truncate_admin,
)
from jobtools import admin_url, engine_for
from ownvitals.admin import db as admin_db
from ownvitals.admin.audit import RefusalLimiter, Target, UnknownAction, checked_params
from ownvitals.admin.db import (
    Actor,
    AdminDb,
    AdminRefused,
    AdminTransactionAborted,
    MaintRoleError,
    admin_session,
    audit_refusal,
    role_problems,
)

IP_HASH = bytes(32)


def _superuser(sql: str, **params: object) -> list[Any]:
    with engine_for(admin_url()) as engine, engine.begin() as connection:
        result = connection.execute(text(sql), params)
        return list(result.all()) if result.returns_rows else []


def _new_user(email: str, role: str = "admin") -> uuid.UUID:
    user_id = uuid.uuid4()
    _superuser(
        "INSERT INTO users (id, email, email_normalized, email_verified_at, role) "
        "VALUES (:i, :e, lower(:e), now(), :r)",
        i=user_id,
        e=email,
        r=role,
    )
    return user_id


def _rows() -> list[Any]:
    return _superuser("SELECT * FROM admin_audit ORDER BY id")


@pytest.fixture(name="db")
async def db_fixture(admin_schema: str) -> AsyncIterator[AdminDb]:
    truncate_admin(admin_url())
    found = AdminDb(make_url(admin_schema))
    try:
        await found.verify()
        yield found
    finally:
        await found.dispose()


@pytest.fixture(name="actor")
def actor_fixture(db: AdminDb) -> Actor:
    del db
    return Actor(_new_user("admin@example.com"), "admin@example.com", "req-1", IP_HASH)


async def test_a_read_writes_its_row_first_and_cannot_write(db: AdminDb, actor: Actor) -> None:
    with pytest.raises(AdminTransactionAborted):  # noqa: PT012 - the session's body is the call
        async with admin_session(db, actor, "setting.read") as session:
            assert (await session.execute(text("SHOW transaction_read_only"))).scalar() == "on"
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await session.execute(
                    text("INSERT INTO app_settings (flag, value) VALUES ('banner', 'null')")
                )
    # The failed statement aborted the transaction (its ``ok`` row goes with it), and the caught
    # error does not turn into a silent commit: one error row.
    rows = _rows()
    assert [(r.action, r.outcome, r.reason) for r in rows] == [
        ("setting.read", "error", "internal")
    ]
    assert rows[0].actor_id == actor.user_id
    assert rows[0].ip_hash == IP_HASH


async def test_a_read_that_succeeds_leaves_one_ok_row(db: AdminDb, actor: Actor) -> None:
    async with admin_session(db, actor, "job.list", params={"days": 7}) as session:
        await session.execute(text("SELECT count(*) FROM jobs"))
    (row,) = _rows()
    assert (row.action, row.outcome, row.params, row.request_id) == (
        "job.list",
        "ok",
        {"days": 7},
        "req-1",
    )


async def test_a_write_commits_with_its_row(db: AdminDb, actor: Actor) -> None:
    async with admin_session(
        db, actor, "setting.set", write=True, params={"key": "signup_mode", "value": "closed"}
    ) as session:
        await session.execute(
            text("INSERT INTO app_settings (flag, value) VALUES ('signup_mode', '\"closed\"')")
        )
    assert [r.outcome for r in _rows()] == ["ok"]
    assert _superuser("SELECT value FROM app_settings")[0].value == "closed"


async def test_a_refused_write_changes_nothing_and_leaves_a_refused_row(
    db: AdminDb, actor: Actor
) -> None:
    with pytest.raises(AdminRefused) as refused:  # noqa: PT012 - the session's body is the call
        async with admin_session(
            db, actor, "setting.set", write=True, params={"key": "banner"}
        ) as session:
            await session.execute(
                text("INSERT INTO app_settings (flag, value) VALUES ('banner', 'null')")
            )
            raise AdminRefused("invalid_value", 422)
    assert refused.value.audited
    assert _superuser("SELECT count(*) AS n FROM app_settings")[0].n == 0
    assert [(r.outcome, r.reason, r.params) for r in _rows()] == [
        ("refused", "invalid_value", {"key": "banner"})
    ]


async def test_a_failed_write_leaves_an_error_row(db: AdminDb, actor: Actor) -> None:
    with pytest.raises(RuntimeError):
        async with admin_session(db, actor, "setting.clear", write=True):
            raise RuntimeError("boom")
    assert [(r.outcome, r.reason) for r in _rows()] == [("error", "internal")]


async def test_an_unknown_target_user_is_a_404_recorded_with_the_id_as_text(
    db: AdminDb, actor: Actor
) -> None:
    missing = uuid.uuid4()
    with pytest.raises(AdminRefused) as refused:
        async with admin_session(db, actor, "setting.read", target=Target(user_id=missing)):
            pass
    assert (refused.value.status, refused.value.reason) == (404, "not_found")
    (row,) = _rows()
    assert (row.outcome, row.target_user_id, row.target_kind, row.target_id) == (
        "refused",
        None,
        "user",
        str(missing),
    )


async def test_the_session_owns_its_transaction(db: AdminDb, actor: Actor) -> None:
    with pytest.raises(Exception, match="not allowed"):
        async with admin_session(db, actor, "setting.read") as session:
            await session.commit()


async def test_unknown_actions_and_bad_params_are_refused_before_any_connection(
    db: AdminDb, actor: Actor
) -> None:
    for action, params in [
        ("user.delete", None),  # #127's, not listed yet
        ("setting.set", {"key": "banner", "text": "a secret"}),  # free text is no field
        ("job.list", {"days": 0}),
        ("audit.list", {"actor_email": "a\x00b@example.com"}),
    ]:
        with pytest.raises(UnknownAction):
            async with admin_session(db, actor, action, params=params):
                pass
    assert _rows() == []


def test_params_are_canonical_json_within_the_size_bound() -> None:
    assert checked_params("setting.set", {"value": "on", "key": "ai_insights"}) == (
        '{"key":"ai_insights","value":"on"}'
    )
    with pytest.raises(UnknownAction):
        checked_params("audit.list", {"target_id": "x" * 101})


async def test_refusal_rows_are_rate_limited(db: AdminDb, actor: Actor) -> None:
    db.limiter = RefusalLimiter(per_admin=2, total=100, clock=lambda: 0.0)
    params = {"method": "PUT", "route": "/api/admin/settings/{key}"}
    written = [
        await audit_refusal(db, actor, "admin.request", "step_up_required", params=params)
        for _ in range(4)
    ]
    assert written == [True, True, False, False]
    assert len(_rows()) == 2


def test_the_refusal_limiter_counts_per_admin_and_in_total() -> None:
    now = [0.0]
    limiter = RefusalLimiter(per_admin=2, total=3, clock=lambda: now[0])
    a, b = uuid.uuid4(), uuid.uuid4()
    assert [limiter.allow(a), limiter.allow(a), limiter.allow(a)] == [True, True, False]
    assert [limiter.allow(b), limiter.allow(b)] == [True, False]  # the total of 3 is reached
    now[0] = 60.0
    assert limiter.allow(a)


def test_an_actor_needs_a_32_byte_ip_hash() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Actor(uuid.uuid4(), "a@example.com", "r", b"short")


GOOD_ROW: tuple[Any, ...] = (
    "ownvitals_maint",
    False,
    True,
    False,
    False,
    False,
    False,
    True,
    0,
    sorted(f"0:{k}={v}" for k, v in admin_db.EXPECTED_MAINT_SETTINGS.items()),
    False,
)


@pytest.mark.parametrize(
    ("index", "value", "problem"),
    [
        (0, "ownvitals_app", "another role"),
        (1, True, "superuser"),
        (2, False, "BYPASSRLS"),
        (3, True, "create databases"),
        (4, True, "create databases"),
        (5, True, "create databases"),
        (6, True, "inherits"),
        (7, False, "cannot log in"),
        (8, 1, "member"),
        (9, ["0:statement_timeout=30s"], "role settings"),
        (10, True, "owns"),
    ],
)
def test_role_problems_name_each_difference(index: int, value: Any, problem: str) -> None:
    assert role_problems(GOOD_ROW) == []
    row = list(GOOD_ROW)
    row[index] = value
    assert any(problem in found for found in role_problems(row))


def test_a_missing_transaction_timeout_or_read_only_default_is_refused() -> None:
    for drop in ("transaction_timeout", "default_transaction_read_only"):
        row = list(GOOD_ROW)
        row[9] = [s for s in GOOD_ROW[9] if drop not in s]
        assert role_problems(row)


async def test_web_refuses_another_role_on_the_maint_url(
    admin_schema: str, app_database_url: str
) -> None:
    del admin_schema
    wrong = AdminDb(make_url(app_database_url))
    try:
        with pytest.raises(MaintRoleError, match="another role"):
            await wrong.verify()
    finally:
        await wrong.dispose()


async def test_a_changed_role_setting_is_refused_on_the_next_connection(
    admin_schema: str,
) -> None:
    _superuser("ALTER ROLE ownvitals_maint SET statement_timeout = 0")
    try:
        found = AdminDb(make_url(admin_schema))
        try:
            with pytest.raises(MaintRoleError, match="role settings"):
                await found.verify()
        finally:
            await found.dispose()
    finally:
        _superuser("ALTER ROLE ownvitals_maint SET statement_timeout = '30s'")


async def test_a_pooled_connection_with_changed_state_is_replaced(
    db: AdminDb, actor: Actor
) -> None:
    """A session-level change survives neither the check-in reset nor the checkout check."""
    async with admin_session(db, actor, "setting.read") as session:
        await session.execute(text("SET SESSION statement_timeout = 0"))
    async with admin_session(db, actor, "setting.read") as session:
        assert (await session.execute(text("SHOW statement_timeout"))).scalar() == "30s"
        assert (await session.execute(text("SHOW transaction_timeout"))).scalar() == "5min"


def test_check_checkout_refuses_a_connection_left_read_write() -> None:
    class Cursor:
        def execute(self, _sql: str) -> None:
            pass

        def fetchone(self) -> tuple[str, str, list[str]]:
            return ("off", "off", admin_db.EXPECTED_TIMEOUTS_MS)

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def rollback(self) -> None:
            pass

    from sqlalchemy.exc import DisconnectionError

    with pytest.raises(DisconnectionError):
        admin_db.check_checkout(Connection())
