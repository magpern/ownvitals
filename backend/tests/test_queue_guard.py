"""Guard 9 and the worker's startup and per-connection queue check (ADR 0005 section 3).

Every drift case below is planted as the superuser, must be found by guard 9 and refused by the
check the worker (and ``jobs stats``) runs, and is then repaired by running ``20-queue.sql`` again
(so the cases also prove the script repairs what it manages). The web process's refusal is #77
item 3.
"""

import argparse
import io
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from jobcore import settings_for
from jobtools import (  # noqa: F401 - fixtures
    QUEUE_SCRIPT,
    admin_fixture,
    engine_for,
    install_queue,
    new_job,
    new_user,
    queue_schema_fixture,
)
from ownvitals.cli import run_jobs
from ownvitals.db import DatabaseRoleError, create_engine
from ownvitals.db.queue_check import (
    COLUMN_COUNT,
    QUEUE_QUERY,
    QueueDriftError,
    install_queue_check,
    queue_problems,
    queue_verified,
    verify_queue,
)
from ownvitals.worker.__main__ import _run  # pyright: ignore[reportPrivateUsage]
from ownvitals.worker.loop import create_worker_engine, verify_startup
from queue_guard import check_queue

CLAIM_SIG = "public.queue_claim(text,integer,text[])"
STATS = argparse.Namespace(command="jobs", action="stats")
DRIFTED = "job queue does not match ADR 0005"


@pytest.fixture
def superuser(queue_schema: str) -> Iterator[str]:
    """The admin URL; the queue is installed again afterwards, whatever a test planted."""
    yield queue_schema
    install_queue(queue_schema)


async def startup_and_connection_problems(app_database_url: str) -> tuple[str, str]:
    """The messages of the startup check and of the per-connection check ('' when they pass)."""
    messages: list[str] = []
    engine = create_worker_engine(settings_for(app_database_url))
    try:
        try:
            await verify_startup(engine)
            messages.append("")
        except DatabaseRoleError as exc:
            messages.append(str(exc))
        await engine.dispose()  # the next connection is a new one
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            messages.append("")
        except DatabaseRoleError as exc:
            messages.append(str(exc))
    finally:
        await engine.dispose()
    return messages[0], messages[1]


async def test_guard_9_and_the_startup_check_pass_on_the_installed_queue(
    admin: Connection, app_database_url: str
) -> None:
    assert check_queue(admin) == []
    assert await startup_and_connection_problems(app_database_url) == ("", "")


def test_the_script_is_plain_sql_and_refuses_a_non_superuser(
    queue_schema: str, owner_database_url: str
) -> None:
    script = QUEUE_SCRIPT.read_text(encoding="utf-8")
    assert not [line for line in script.splitlines() if line.startswith("\\")]
    # psql --single-transaction makes the one transaction.
    assert "BEGIN;" not in script
    assert "COMMIT;" not in script
    with pytest.raises(Exception, match="must run as the superuser"):
        install_queue(owner_database_url)


def test_the_script_is_idempotent(admin: Connection, superuser: str) -> None:
    install_queue(superuser)
    install_queue(superuser)
    assert check_queue(admin) == []


Q = "ownvitals_queue"
DRIFT = [
    ("GRANT EXECUTE ON FUNCTION public.queue_stats() TO PUBLIC", "queue_stats"),
    ("GRANT EXECUTE ON FUNCTION public.queue_stats() TO ownvitals_owner", "queue_stats"),
    (f"REVOKE EXECUTE ON FUNCTION {CLAIM_SIG} FROM {Q}", "queue_claim"),
    (f"REVOKE EXECUTE ON FUNCTION {CLAIM_SIG} FROM ownvitals_app", "queue_claim"),
    ("ALTER FUNCTION public.queue_reap(integer) SECURITY INVOKER", "queue_reap"),
    (f"ALTER FUNCTION {CLAIM_SIG} SET search_path = public, pg_temp", "queue_claim"),
    ("COMMENT ON FUNCTION public.queue_stats() IS 'ownvitals-queue v0'", "queue_stats"),
    ("ALTER FUNCTION public.queue_reap(integer) OWNER TO ownvitals_owner", "queue_reap"),
    (
        "CREATE OR REPLACE FUNCTION public.queue_stats() RETURNS TABLE (metric text, total bigint)"
        " LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
        " AS $$ BEGIN RETURN QUERY SELECT 'x', 0::bigint; END $$",
        "queue_stats",
    ),
    (
        "CREATE FUNCTION public.queue_stats(p integer) RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "queue_stats",
    ),
    (f"GRANT DELETE ON public.jobs TO {Q}", Q),
    (f"REVOKE UPDATE ON public.jobs FROM {Q}", Q),
    (f"GRANT UPDATE ON public.job_events TO {Q}", Q),
    (f"GRANT SELECT (kind) ON public.jobs TO {Q}", Q),
    (f"GRANT CREATE ON SCHEMA public TO {Q}", Q),
    (f"GRANT CONNECT ON DATABASE postgres TO {Q}", Q),
    (f"GRANT pg_read_all_data TO {Q}", Q),
    (f"GRANT {Q} TO ownvitals_owner", Q),
    (f"ALTER ROLE {Q} LOGIN", Q),
    (f"ALTER ROLE {Q} NOBYPASSRLS", Q),
    (f"ALTER ROLE {Q} INHERIT", Q),
    (f"ALTER ROLE {Q} SET work_mem = '64MB'", Q),
]


@pytest.mark.parametrize(("statement", "subject"), DRIFT)
async def test_drift_is_found_refused_and_repaired(
    admin: Connection, superuser: str, app_database_url: str, statement: str, subject: str
) -> None:
    admin.execute(text(statement))
    try:
        found = check_queue(admin)
        assert any(subject in problem for problem in found), found
        startup, connection = await startup_and_connection_problems(app_database_url)
        assert DRIFTED in startup
        assert DRIFTED in connection
        assert subject in startup
    finally:
        install_queue(superuser)
    assert check_queue(admin) == []
    assert await startup_and_connection_problems(app_database_url) == ("", "")


# User code a queue function could fire, or an expression calling a built-in that runs SQL text
# (ADR 0005 section 3: "No user code runs as the queue role"): (plant, undo, what the startup
# check reports, what guard 9 reports).
HOOKS = [
    (
        [
            "CREATE FUNCTION public.t_hook() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN NEW; END $$",
            "CREATE TRIGGER t_hook BEFORE UPDATE ON public.jobs "
            "FOR EACH ROW EXECUTE FUNCTION public.t_hook()",
        ],
        ["DROP TRIGGER t_hook ON public.jobs", "DROP FUNCTION public.t_hook()"],
        "trigger t_hook on public.jobs",
        "trigger t_hook on public.jobs",
    ),
    (
        ["CREATE RULE t_rule AS ON INSERT TO public.job_events DO ALSO NOTIFY t_rule"],
        ["DROP RULE t_rule ON public.job_events"],
        "rule t_rule on public.job_events",
        "rule t_rule on public.job_events",
    ),
    (
        [
            "ALTER TABLE public.jobs ALTER COLUMN leased_by "
            "SET DEFAULT set_config('search_path', 'public', true)"
        ],
        ["ALTER TABLE public.jobs ALTER COLUMN leased_by DROP DEFAULT"],
        "built-in set_config in the default of public.jobs.leased_by",
        "built-in set_config in the default of jobs.leased_by",
    ),
]


@pytest.mark.parametrize(("plant", "undo", "reported", "guarded"), HOOKS)
async def test_user_code_a_queue_function_could_fire_is_refused(
    admin: Connection,
    superuser: str,
    app_database_url: str,
    plant: list[str],
    undo: list[str],
    reported: str,
    guarded: str,
) -> None:
    for statement in plant:
        admin.execute(text(statement))
    try:
        found = check_queue(admin)
        assert any(guarded in problem for problem in found), found
        startup, connection = await startup_and_connection_problems(app_database_url)
        assert reported in startup
        assert DRIFTED in connection
        with pytest.raises(Exception, match="besides the queue functions"):
            install_queue(superuser)
    finally:
        for statement in undo:
            admin.execute(text(statement))


def test_the_check_reports_a_missing_role_and_a_malformed_row() -> None:
    missing = queue_problems((None, 0, 0, [], [], [], []))
    assert missing == [
        "the role ownvitals_queue does not exist (run docker/postgres/queue/20-queue.sql)"
    ]
    with pytest.raises(ValueError, match=f"{COLUMN_COUNT} columns"):
        queue_problems((None,))
    assert "ownvitals_queue" in QUEUE_QUERY


async def test_a_database_down_at_startup_is_not_verified_until_a_connection_passes(
    admin: Connection, unreachable_database_url: str, app_database_url: str
) -> None:
    down = create_worker_engine(settings_for(unreachable_database_url))
    try:
        await verify_startup(down)  # no error: nothing to check yet
        assert not queue_verified(down)  # so the worker claims nothing
    finally:
        await down.dispose()
    engine = create_worker_engine(settings_for(app_database_url))
    try:
        assert not queue_verified(engine)
        async with engine.connect() as connection:  # the per-connection check passes
            await connection.execute(text("SELECT 1"))
        assert queue_verified(engine)
    finally:
        await engine.dispose()


async def test_verify_queue_needs_the_per_connection_check(app_database_url: str) -> None:
    engine = create_engine(settings_for(app_database_url))
    try:
        with pytest.raises(RuntimeError, match="install_queue_check"):
            await verify_queue(engine)
        install_queue_check(engine)
        install_queue_check(engine)  # once only
    finally:
        await engine.dispose()


async def test_the_worker_and_jobs_stats_refuse_a_drifted_queue(
    admin: Connection, superuser: str, app_database_url: str
) -> None:
    admin.execute(text("GRANT EXECUTE ON FUNCTION public.queue_stats() TO PUBLIC"))
    with pytest.raises(QueueDriftError, match="job queue"):
        await _run(settings_for(app_database_url))
    out, err = io.StringIO(), io.StringIO()
    assert await run_jobs(STATS, settings_for(app_database_url), out, err) == 1
    assert out.getvalue() == ""
    assert DRIFTED in err.getvalue()


async def test_jobs_stats_prints_counts_only(admin: Connection, app_database_url: str) -> None:
    owner = new_user()
    new_job(admin, owner)
    new_job(admin, owner, state="cancelled", finished_at="2026-10-31")
    out, err = io.StringIO(), io.StringIO()
    assert await run_jobs(STATS, settings_for(app_database_url), out, err) == 0
    assert out.getvalue().splitlines() == [
        "jobs_queued 1",
        "jobs_running 0",
        "jobs_completed 0",
        "jobs_failed 0",
        "jobs_cancelled 1",
    ]
    assert str(owner) not in out.getvalue() + err.getvalue()


# Exact-type matches for built-ins the function bodies call. With public on the functions'
# search_path they would beat pg_catalog's versions and run as ownvitals_queue.
SHADOWS = (
    "CREATE FUNCTION public.t_shadow() RETURNS int LANGUAGE plpgsql "
    "AS $$ BEGIN RAISE EXCEPTION 'shadow ran as %', current_user; END $$",
    "CREATE FUNCTION public.cardinality(text[]) RETURNS int LANGUAGE sql "
    "AS 'SELECT public.t_shadow()'",
    "CREATE FUNCTION public.make_interval(secs integer) RETURNS interval LANGUAGE sql "
    "AS 'SELECT public.t_shadow() * interval ''1 second'''",
    "CREATE FUNCTION public.power(integer, integer) RETURNS integer LANGUAGE sql "
    "AS 'SELECT public.t_shadow()'",
    "CREATE FUNCTION public.jsonb_build_object(text, integer) RETURNS jsonb LANGUAGE sql "
    "AS 'SELECT to_jsonb(public.t_shadow())'",
    "CREATE FUNCTION public.t_lt(timestamptz, timestamptz) RETURNS boolean LANGUAGE sql "
    "AS 'SELECT public.t_shadow() > 0'",
    "CREATE OPERATOR public.< "
    "(LEFTARG = timestamptz, RIGHTARG = timestamptz, FUNCTION = public.t_lt)",
)
UNSHADOW = (
    "DROP OPERATOR IF EXISTS public.< (timestamptz, timestamptz)",
    "DROP FUNCTION IF EXISTS public.t_lt(timestamptz, timestamptz)",
    "DROP FUNCTION IF EXISTS public.cardinality(text[])",
    "DROP FUNCTION IF EXISTS public.make_interval(integer)",
    "DROP FUNCTION IF EXISTS public.power(integer, integer)",
    "DROP FUNCTION IF EXISTS public.jsonb_build_object(text, integer)",
    "DROP FUNCTION IF EXISTS public.t_shadow()",
)


async def test_objects_in_public_cannot_shadow_built_ins_and_are_refused(
    admin: Connection, superuser: str, owner_database_url: str, app_database_url: str
) -> None:
    owner = new_user()
    job_id = new_job(admin, owner)
    with engine_for(owner_database_url, isolation_level="AUTOCOMMIT") as engine:
        with engine.connect() as connection:  # as a migration would create them
            for statement in SHADOWS:
                connection.execute(text(statement))
        try:
            found = check_queue(admin)
            assert any("cardinality(text[])" in p for p in found), found
            startup, connection_check = await startup_and_connection_problems(app_database_url)
            assert "cardinality(text[])" in startup
            assert DRIFTED in connection_check
            with pytest.raises(Exception, match=r"the database holds function public\.cardinality"):
                install_queue(superuser)
            # The bodies resolve built-ins in pg_catalog only: no shadow runs (a plain app-role
            # engine, without the queue check, calls them).
            with engine_for(app_database_url) as app, app.begin() as tx:
                [row] = tx.execute(
                    text("SELECT * FROM public.queue_claim('w1', 60, ARRAY['test_kind'])")
                ).all()
                assert row.job_id == job_id
            admin.execute(
                text("UPDATE jobs SET lease_expires_at = now() - interval '1 s' WHERE id = :id"),
                {"id": job_id},
            )
            with engine_for(app_database_url) as app, app.begin() as tx:
                assert tx.execute(text("SELECT public.queue_reap(10)")).scalar_one() == 1
                assert tx.execute(text("SELECT count(*) FROM public.queue_stats()")).scalar() == 5
        finally:
            with engine.connect() as connection:
                for statement in UNSHADOW:
                    connection.execute(text(statement))
    install_queue(superuser)
    assert check_queue(admin) == []
    assert await startup_and_connection_problems(app_database_url) == ("", "")


DEFAULT_EXECUTE = "EXECUTE ON FUNCTIONS"


@pytest.mark.parametrize(
    ("plant", "undo", "message"),
    [
        (
            [
                "CREATE TABLE public.t_queue_owned (id int)",
                f"ALTER TABLE public.t_queue_owned OWNER TO {Q}",
            ],
            ["DROP TABLE public.t_queue_owned"],
            f"{Q} owns table t_queue_owned",
        ),
        (
            [f"ALTER DEFAULT PRIVILEGES FOR ROLE {Q} REVOKE {DEFAULT_EXECUTE} FROM PUBLIC"],
            [f"ALTER DEFAULT PRIVILEGES FOR ROLE {Q} GRANT {DEFAULT_EXECUTE} TO PUBLIC"],
            f"{Q} owns default privileges on new functions",
        ),
    ],
)
async def test_the_script_stops_on_what_it_must_not_repair(
    admin: Connection,
    superuser: str,
    app_database_url: str,
    plant: list[str],
    undo: list[str],
    message: str,
) -> None:
    for statement in plant:
        admin.execute(text(statement))
    try:
        assert check_queue(admin) != []
        startup, _ = await startup_and_connection_problems(app_database_url)
        assert DRIFTED in startup
        with pytest.raises(Exception, match=message):
            install_queue(superuser)
    finally:
        for statement in undo:
            admin.execute(text(statement))


@pytest.mark.usefixtures("restore_logging")
def test_python_m_ownvitals_jobs_stats(
    admin: Connection,
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ownvitals.__main__ import main

    new_job(admin, new_user())
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    assert main(["jobs", "stats"]) == 0
    assert "jobs_queued 1" in capsys.readouterr().out.splitlines()
    with pytest.raises(SystemExit) as refused:
        main(["jobs"])  # an action is required
    assert refused.value.code == 2
