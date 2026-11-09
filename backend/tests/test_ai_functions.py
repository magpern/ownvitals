"""The AI cost cap's definer family (ADR 0014 section 7, ADR 0005 section 3, #205): ``ai_reserve``,
``ai_settle``, ``ai_release`` and ``ai_settle_lost``, their role ``ownvitals_ai``, and the
startup check that pins them. Synthetic users only; no content.

- each function is executable by the app role and by no other caller role
  (``test_each_function_is_the_app_role_s_only``);
- none takes an owner id (``test_the_signatures_take_no_owner``);
- the role has exactly its attributes and grants (guard 2), the bodies their shipped digests
  (guard 9 and the startup check of the worker), and the script is plain SQL, idempotent and
  refuses a non-superuser;
- a drifted function, a dropped one or a drifted role is found, refused at startup, and
  repaired by running the script again (``test_drift_*``, ``test_guard_2_*``).
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from aitools import ai_fixture, running_job  # noqa: F401 - fixtures
from dbtools import AI_SCRIPT
from jobcore import settings_for
from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    admin_url,
    engine_for,
    queue_schema_fixture,
    run_script,
)
from ownvitals.db import DatabaseRoleError
from ownvitals.db.queue_check import AI_FUNCTION_NAMES, AI_PINS, QUEUE_QUERY, queue_problems
from ownvitals.db.queue_digests import QUEUE_DIGESTS
from ownvitals.worker.loop import create_worker_engine, verify_startup
from queue_guard import check_queue
from role_guard import check_roles

AI = "ownvitals_ai"
REFUSED = "42501"
UNDEFINED = "42883"
DRIFTED = "job queue does not match ADR 0005"
CALLS = {
    "ai_reserve": ("SELECT ai_reserve(1, :id, 0, 0)", {"id": uuid.uuid4()}),
    "ai_settle": ("SELECT ai_settle(:id, 0)", {"id": uuid.uuid4()}),
    "ai_release": ("SELECT ai_release(:id)", {"id": uuid.uuid4()}),
    "ai_settle_lost": ("SELECT ai_settle_lost(1)", {}),
}
OTHER_ROLES = (
    "ownvitals_owner",
    "ownvitals_maint",
    "ownvitals_queue",
    "ownvitals_webhook_events",
    "ownvitals_webhook_secrets",
    "ownvitals_metrics",
    "ownvitals_test_public",
)


@pytest.fixture(name="reinstall")
def reinstall_fixture(queue_schema: str) -> Iterator[str]:
    """The admin URL; 50-ai.sql runs again afterwards, whatever a test planted."""
    yield queue_schema
    run_script(queue_schema, AI_SCRIPT.read_text(encoding="utf-8"))


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


async def startup_problems(app_database_url: str) -> str:
    engine = create_worker_engine(settings_for(app_database_url))
    try:
        await verify_startup(engine)
    except DatabaseRoleError as exc:
        return str(exc)
    finally:
        await engine.dispose()
    return ""


@pytest.mark.parametrize("name", AI_FUNCTION_NAMES)
def test_each_function_is_the_app_role_s_only(
    ai: Connection, app_database_url: str, name: str
) -> None:
    """The app role (the worker) may call it; the owner, maint, the other definer roles and a
    role with only PUBLIC's rights may not."""
    sql, params = CALLS[name]
    user = uuid.uuid4()
    with engine_for(app_database_url) as engine, engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user)})
        if name == "ai_reserve":
            # The app role reaches the function; without a running job it refuses the input.
            with pytest.raises(DBAPIError) as refused:
                connection.execute(text(sql), params)
            assert _sqlstate(refused.value) == "22023"
        else:
            connection.execute(text(sql), params)
    ai.execute(text("DROP ROLE IF EXISTS ownvitals_test_public"))
    ai.execute(text("CREATE ROLE ownvitals_test_public NOLOGIN"))
    try:
        for role in OTHER_ROLES:
            usage = ai.execute(
                text("SELECT has_schema_privilege(:r, 'public', 'USAGE')"), {"r": role}
            ).scalar()
            with engine_for(admin_url()) as engine, engine.connect() as connection:
                connection.execute(text(f"SET ROLE {role}"))
                with pytest.raises(DBAPIError) as raised:
                    connection.execute(text(sql), params)
                # Without USAGE on public the function is not even found.
                assert _sqlstate(raised.value) == (REFUSED if usage else UNDEFINED), role
    finally:
        ai.execute(text("DROP ROLE IF EXISTS ownvitals_test_public"))


def test_the_signatures_take_no_owner(ai: Connection) -> None:
    rows = dict(
        ai.execute(
            text(
                "SELECT proname, pg_get_function_identity_arguments(oid) FROM pg_proc "
                "WHERE proname = ANY(:n)"
            ),
            {"n": list(AI_FUNCTION_NAMES)},
        ).all()
    )
    assert rows == {name: pin[0] for name, pin in AI_PINS.items()}
    assert not [args for args in rows.values() if "owner" in args or "user_id" in args]


async def test_the_installed_family_passes_every_check(
    ai: Connection, app_database_url: str
) -> None:
    assert check_roles(ai) == []
    assert check_queue(ai) == []
    assert queue_problems(ai.execute(text(QUEUE_QUERY)).one()) == []
    assert await startup_problems(app_database_url) == ""
    digests = dict(
        ai.execute(
            text(
                "SELECT proname, md5(pg_get_functiondef(oid)) FROM pg_proc WHERE proname = ANY(:n)"
            ),
            {"n": list(AI_FUNCTION_NAMES)},
        ).all()
    )
    assert digests == {name: QUEUE_DIGESTS[name] for name in AI_FUNCTION_NAMES}


def test_the_script_is_plain_sql_idempotent_and_refuses_a_non_superuser(
    ai: Connection, reinstall: str, owner_database_url: str
) -> None:
    script = AI_SCRIPT.read_text(encoding="utf-8")
    assert not [line for line in script.splitlines() if line.startswith("\\")]
    assert "BEGIN;" not in script
    assert "COMMIT;" not in script
    with pytest.raises(Exception, match="must run as the superuser"):
        run_script(owner_database_url, script)
    run_script(reinstall, script)
    run_script(reinstall, script)
    assert check_roles(ai) == []
    assert check_queue(ai) == []


R = "public.ai_reserve(bigint, uuid, bigint, bigint)"
DRIFT = [
    (f"GRANT EXECUTE ON FUNCTION {R} TO PUBLIC", "ai_reserve"),
    (f"GRANT EXECUTE ON FUNCTION {R} TO ownvitals_owner", "ai_reserve"),
    (f"REVOKE EXECUTE ON FUNCTION {R} FROM ownvitals_app", "ai_reserve"),
    (f"ALTER FUNCTION {R} SECURITY INVOKER", "ai_reserve"),
    (f"ALTER FUNCTION {R} SET search_path = public, pg_temp", "ai_reserve"),
    (f"COMMENT ON FUNCTION {R} IS 'ownvitals-ai v0'", "ai_reserve"),
    (f"ALTER FUNCTION {R} OWNER TO ownvitals_owner", "ai_reserve"),
    # A body that admits everything: same signature, settings and owner, another digest.
    (
        "CREATE OR REPLACE FUNCTION public.ai_settle(p_id uuid, p_actual bigint) RETURNS boolean"
        " LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
        " AS $$ BEGIN RETURN true; END $$",
        "ai_settle",
    ),
    # One function of an installed family dropped.
    ("DROP FUNCTION public.ai_settle_lost(integer)", "ai_settle_lost"),
    # An overload beside the shipped one.
    (
        "CREATE FUNCTION public.ai_release(p_id uuid, p_force boolean) RETURNS boolean"
        " LANGUAGE sql AS $$ SELECT true $$",
        "ai_release",
    ),
]


@pytest.mark.parametrize(("statement", "subject"), DRIFT)
async def test_drift_is_found_refused_and_repaired(
    ai: Connection, reinstall: str, app_database_url: str, statement: str, subject: str
) -> None:
    ai.execute(text(statement))
    try:
        assert any(subject in problem for problem in check_queue(ai))
        found = await startup_problems(app_database_url)
        assert DRIFTED in found
        assert subject in found
    finally:
        run_script(reinstall, AI_SCRIPT.read_text(encoding="utf-8"))
    assert check_queue(ai) == []
    assert await startup_problems(app_database_url) == ""


ROLE_DRIFT = [
    f"ALTER ROLE {AI} LOGIN",
    f"ALTER ROLE {AI} NOBYPASSRLS",
    f"ALTER ROLE {AI} INHERIT",
    f"ALTER ROLE {AI} SET work_mem = '64MB'",
    f"GRANT pg_read_all_data TO {AI}",
    f"GRANT {AI} TO ownvitals_owner",
    f"GRANT SELECT ON public.users TO {AI}",
    f"GRANT SELECT (input) ON public.jobs TO {AI}",
    f"GRANT DELETE ON public.ai_reservations TO {AI}",
    f"GRANT UPDATE (amount) ON public.ai_reservations TO {AI}",
    f"GRANT UPDATE (month) ON public.ai_spend_global TO {AI}",
    f"GRANT CREATE ON SCHEMA public TO {AI}",
    "GRANT SELECT ON public.ai_spend_global TO ownvitals_app",
    "GRANT UPDATE ON public.ai_spend TO ownvitals_app",
    "GRANT INSERT (owner_id) ON public.ai_reservations TO ownvitals_app",
]


@pytest.mark.parametrize("statement", ROLE_DRIFT)
def test_guard_2_finds_a_drifted_role_and_the_script_repairs_it(
    ai: Connection, reinstall: str, statement: str
) -> None:
    ai.execute(text(statement))
    try:
        assert check_roles(ai) != []
    finally:
        run_script(reinstall, AI_SCRIPT.read_text(encoding="utf-8"))
    assert check_roles(ai) == []
