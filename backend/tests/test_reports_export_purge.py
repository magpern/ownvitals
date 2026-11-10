"""The export's cross-user purge (ADR 0013 section 4; #333): ``exports_purge_expired()`` in
60-exports.sql, its role ``ownvitals_exports``, ``ownvitals.reports.export_purge`` and the
worker's hourly purge tick.

Synthetic data only: owners are random UUIDs with ``example.test`` addresses, chunks hold
made-up bytes, no content.

- for two owners: ready exports past ``expires_at`` become expired, the chunks of failed and
  expired exports go, failed and expired rows older than 90 days go; a live ready export, any
  build (even one whose job ended: the owner's tidy fails it) and a younger record stay;
- it writes no audit row (ADR 0013 section 4 literally; the role holds nothing on
  ``auth_events``);
- it returns three counts and nothing else, takes no argument, and a caller's ``app.user_id``
  neither widens nor narrows it; the app role cannot do any of it itself; rows a download holds
  are skipped; chunks go oldest export first;
- the role's attributes and grants (guard 2), the pin (guard 9, the startup check), the script's
  idempotence and refusal of a non-superuser; drift is found and repaired;
- the Python layer calls again while a call did something, does nothing when the script is not
  installed, and the worker's purge tick runs it.
"""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from dbtools import EXPORTS_SCRIPT, drop_exports_functions
from jobcore import JobsEnv, jenv_fixture, settings_for  # noqa: F401 - fixtures
from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    admin_url,
    engine_for,
    ensure_user,
    new_job,
    queue_schema_fixture,
    run_script,
)
from ownvitals.db import DatabaseRoleError
from ownvitals.db.queue_check import EXPORTS_PINS, QUEUE_QUERY, queue_problems
from ownvitals.db.queue_digests import QUEUE_DIGESTS
from ownvitals.reports import export_purge
from ownvitals.reports.export_purge import purge_expired_exports
from ownvitals.worker import upkeep
from ownvitals.worker.loop import create_worker_engine, verify_startup
from queue_guard import check_queue
from role_guard import check_roles

ROLE = "ownvitals_exports"
PURGE = "SELECT expired, chunks, deleted FROM exports_purge_expired()"
DRIFTED = "job queue does not match ADR 0005"
F = "public.exports_purge_expired()"


@pytest.fixture(name="xp")
def xp_fixture(admin: Connection) -> Iterator[Connection]:
    """The superuser connection on empty export tables and no export audit rows."""
    _empty(admin)
    yield admin
    _empty(admin)


def _empty(su: Connection) -> None:
    su.execute(text("TRUNCATE export_chunks, exports"))
    su.execute(text("DELETE FROM auth_events WHERE kind LIKE 'export_%'"))


@pytest.fixture(name="reinstall")
def reinstall_fixture(queue_schema: str) -> Iterator[str]:
    """The admin URL; 60-exports.sql runs again afterwards, whatever a test planted."""
    yield queue_schema
    run_script(queue_schema, EXPORTS_SCRIPT.read_text(encoding="utf-8"))


def plant(
    su: Connection,
    owner: uuid.UUID,
    state: str,
    *,
    expires: str | None = None,
    age: str = "0 seconds",
    job_state: str | None = None,
    chunks: int = 2,
    kind: str = "data",
) -> uuid.UUID:
    """One synthetic export of ``owner`` in ``state`` with ``chunks`` made-up chunks.
    ``expires`` is an interval from now (negative: past), ``age`` how long ago it was created,
    ``job_state`` the state of its build job."""
    ensure_user(su, owner)
    job = _job(su, owner, job_state) if job_state is not None else None
    ready = state in ("ready", "expired")
    export_id = su.execute(
        text(
            "INSERT INTO exports (owner_id, kind, state, job_id, failure, created_at, ready_at, "
            "expires_at, size, sha256) VALUES (:owner, :kind, :state, :job, :failure, "
            "now() - CAST(:age AS interval), CASE WHEN :ready THEN now() END, "
            "now() + CAST(:expires AS interval), CASE WHEN :ready THEN 10 END, "
            "CASE WHEN :ready THEN :sha END) RETURNING id"
        ),
        {
            "owner": owner,
            "kind": kind,
            "state": state,
            "job": job,
            "failure": "build_failed" if state == "failed" else None,
            "age": age,
            "ready": ready,
            "expires": expires if expires is not None else ("1 day" if ready else None),
            "sha": bytes(32),
        },
    ).scalar_one()
    for seq in range(chunks):
        su.execute(
            text(
                "INSERT INTO export_chunks (owner_id, export_id, seq, bytes) "
                "VALUES (:owner, :id, :seq, :b)"
            ),
            {"owner": owner, "id": export_id, "seq": seq, "b": b"synthetic"},
        )
    return export_id


def _job(su: Connection, owner: uuid.UUID, state: str) -> uuid.UUID:
    """A build job of ``owner`` in ``state``, with the columns that state requires."""
    columns: dict[str, Any] = {"state": state}
    if state in ("completed", "failed", "dead_letter", "cancelled"):
        columns["finished_at"] = datetime.now(UTC)
    if state in ("failed", "dead_letter"):
        columns["error_category"] = "internal"
    return new_job(su, owner, **columns)


def states(su: Connection) -> dict[uuid.UUID, tuple[str, str | None, int]]:
    """Each export's state, failure and number of chunks."""
    rows = su.execute(
        text(
            "SELECT e.id, e.state, e.failure, (SELECT count(*) FROM export_chunks c "
            "WHERE c.export_id = e.id) AS chunks FROM exports e"
        )
    ).all()
    return {r.id: (r.state, r.failure, int(r.chunks)) for r in rows}


def audit_rows(su: Connection) -> int:
    return int(su.execute(text("SELECT count(*) FROM auth_events")).scalar_one())


def call(
    app_database_url: str, user: uuid.UUID | None = None, *, lock_timeout: bool = False
) -> Any:
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        if lock_timeout:  # a step that waited on a held row would fail instead of hanging
            conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        if user is not None:
            conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user)})
        return conn.execute(text(PURGE)).one()


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


def test_the_purge_covers_every_owner_and_keeps_what_is_live(
    xp: Connection, app_database_url: str
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    gone_a = plant(xp, alice, "ready", expires="-1 minute")
    gone_b = plant(xp, bob, "ready", expires="-2 hours", kind="doctor_pdf")
    ended = plant(xp, bob, "building", job_state="dead_letter")
    failed = plant(xp, alice, "failed")
    old = plant(xp, alice, "expired", age="91 days", chunks=0)
    old_failed = plant(xp, bob, "failed", age="90 days 1 hour")
    live = plant(xp, alice, "ready", expires="1 minute")
    running = plant(xp, bob, "building", job_state="queued", kind="doctor_pdf")
    young = plant(xp, bob, "expired", age="89 days", chunks=0)

    row = call(app_database_url)
    assert tuple(row) == (2, 2 * 2 + 2 + 2, 2)
    assert list(row._fields) == ["expired", "chunks", "deleted"]
    assert states(xp) == {
        gone_a: ("expired", None, 0),
        gone_b: ("expired", None, 0),
        # Not the purge's: the owner's tidy fails a build whose job ended (ADR 0013 section 4).
        ended: ("building", None, 2),
        failed: ("failed", "build_failed", 0),
        live: ("ready", None, 2),
        running: ("building", None, 2),
        young: ("expired", None, 0),
    }
    assert old not in states(xp)
    assert old_failed not in states(xp)
    # A second call finds nothing more.
    assert tuple(call(app_database_url)) == (0, 0, 0)


def test_it_writes_no_audit_row(xp: Connection, app_database_url: str) -> None:
    before = audit_rows(xp)
    plant(xp, uuid.uuid4(), "ready", expires="-1 minute")
    plant(xp, uuid.uuid4(), "failed", age="91 days")
    assert tuple(call(app_database_url)) == (1, 4, 1)
    assert audit_rows(xp) == before


def test_an_export_a_download_holds_is_skipped_until_the_next_call(
    xp: Connection, app_database_url: str, queue_schema: str
) -> None:
    held = plant(xp, uuid.uuid4(), "ready", expires="-1 minute")
    free = plant(xp, uuid.uuid4(), "ready", expires="-1 minute")
    with engine_for(queue_schema) as engine, engine.connect() as other:
        other.begin()
        # What begin_download and take_last_chunk do: lock the export row.
        other.execute(text("SELECT id FROM exports WHERE id = :id FOR UPDATE"), {"id": held})
        assert tuple(call(app_database_url, lock_timeout=True)) == (1, 2, 0)
        assert states(xp)[held] == ("ready", None, 2)
        other.rollback()
    assert tuple(call(app_database_url)) == (1, 2, 0)
    assert states(xp)[held] == states(xp)[free] == ("expired", None, 0)


def test_chunks_go_oldest_export_first(xp: Connection, app_database_url: str) -> None:
    owner = uuid.uuid4()
    newer = plant(xp, owner, "failed", age="1 day", chunks=600)
    older = plant(xp, owner, "failed", age="2 days", chunks=600)
    assert call(app_database_url).chunks == 1000
    assert states(xp)[older][2] == 0
    assert states(xp)[newer][2] == 200
    assert call(app_database_url).chunks == 200


def test_a_user_context_neither_widens_nor_narrows_it(
    xp: Connection, app_database_url: str
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    a = plant(xp, alice, "ready", expires="-1 minute")
    b = plant(xp, bob, "ready", expires="-1 minute")
    assert call(app_database_url, alice).expired == 2
    assert states(xp)[a][0] == states(xp)[b][0] == "expired"


def test_each_step_is_bounded_oldest_first(xp: Connection, app_database_url: str) -> None:
    owner = uuid.uuid4()
    oldest = plant(xp, owner, "ready", expires="-3 days", chunks=0)
    for _ in range(100):
        plant(xp, owner, "ready", expires="-1 minute", chunks=0)
    assert call(app_database_url).expired == 100
    assert states(xp)[oldest][0] == "expired"
    assert call(app_database_url).expired == 1
    assert call(app_database_url).expired == 0


def test_the_signature_takes_nothing_and_the_app_role_cannot_purge_itself(
    xp: Connection, app_database_url: str
) -> None:
    args = xp.execute(
        text(
            "SELECT pg_get_function_identity_arguments(oid), pronargs FROM pg_proc "
            "WHERE proname = 'exports_purge_expired'"
        )
    ).one()
    assert (args[0], args[1]) == (EXPORTS_PINS["exports_purge_expired"][0], 0)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    kept = plant(xp, bob, "ready", expires="-1 minute")
    # In another user's context, or in none, the app role reaches no row of bob's (forced RLS).
    with engine_for(app_database_url) as engine:
        for user in (alice, None):
            with engine.begin() as conn:
                if user is not None:
                    conn.execute(
                        text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user)}
                    )
                changed = conn.execute(
                    text("UPDATE exports SET state = 'expired' WHERE owner_id = :o"), {"o": bob}
                ).rowcount
                gone = conn.execute(
                    text("DELETE FROM export_chunks WHERE owner_id = :o"), {"o": bob}
                ).rowcount
                assert (changed, gone) == (0, 0)
    assert states(xp)[kept] == ("ready", None, 2)


OTHER_ROLES = (
    "ownvitals_owner",
    "ownvitals_maint",
    "ownvitals_queue",
    "ownvitals_ai",
    "ownvitals_webhook_events",
    "ownvitals_test_public",
)


def test_the_app_role_alone_may_call_it(xp: Connection, app_database_url: str) -> None:
    call(app_database_url)
    xp.execute(text("DROP ROLE IF EXISTS ownvitals_test_public"))
    xp.execute(text("CREATE ROLE ownvitals_test_public NOLOGIN"))
    try:
        for role in OTHER_ROLES:
            usage = xp.execute(
                text("SELECT has_schema_privilege(:r, 'public', 'USAGE')"), {"r": role}
            ).scalar()
            with engine_for(admin_url()) as engine, engine.connect() as connection:
                connection.execute(text(f"SET ROLE {role}"))
                with pytest.raises(DBAPIError) as raised:
                    connection.execute(text(PURGE))
                assert _sqlstate(raised.value) == ("42501" if usage else "42883"), role
    finally:
        xp.execute(text("DROP ROLE IF EXISTS ownvitals_test_public"))


async def test_the_installed_purge_passes_every_check(
    xp: Connection, app_database_url: str
) -> None:
    assert check_roles(xp) == []
    assert check_queue(xp) == []
    assert queue_problems(xp.execute(text(QUEUE_QUERY)).one()) == []
    assert await startup_problems(app_database_url) == ""
    digest = xp.execute(
        text(
            "SELECT md5(pg_get_functiondef(oid)) FROM pg_proc "
            "WHERE proname = 'exports_purge_expired'"
        )
    ).scalar_one()
    assert digest == QUEUE_DIGESTS["exports_purge_expired"]


def test_the_script_is_plain_sql_idempotent_and_refuses_a_non_superuser(
    xp: Connection, reinstall: str, owner_database_url: str
) -> None:
    script = EXPORTS_SCRIPT.read_text(encoding="utf-8")
    assert not [line for line in script.splitlines() if line.startswith("\\")]
    assert "BEGIN;" not in script
    assert "COMMIT;" not in script
    with pytest.raises(Exception, match="must run as the superuser"):
        run_script(owner_database_url, script)
    run_script(reinstall, script)
    run_script(reinstall, script)
    assert check_roles(xp) == []
    assert check_queue(xp) == []


DRIFT = [
    f"GRANT EXECUTE ON FUNCTION {F} TO PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {F} TO ownvitals_owner",
    f"REVOKE EXECUTE ON FUNCTION {F} FROM ownvitals_app",
    f"ALTER FUNCTION {F} SECURITY INVOKER",
    f"ALTER FUNCTION {F} SET search_path = public, pg_temp",
    f"COMMENT ON FUNCTION {F} IS 'ownvitals-exports v0'",
    f"ALTER FUNCTION {F} OWNER TO ownvitals_owner",
    # A body that deletes every export at once: same signature, settings and owner.
    "CREATE OR REPLACE FUNCTION public.exports_purge_expired(OUT expired integer, "
    "OUT chunks integer, OUT deleted integer) LANGUAGE plpgsql "
    "SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$ BEGIN "
    "DELETE FROM public.exports; expired := 0; chunks := 0; deleted := 0; END $$",
    # An overload beside the shipped one.
    "CREATE FUNCTION public.exports_purge_expired(p_owner uuid) RETURNS integer "
    "LANGUAGE sql AS $$ SELECT 0 $$",
]


@pytest.mark.parametrize("statement", DRIFT)
async def test_drift_is_found_refused_and_repaired(
    xp: Connection, reinstall: str, app_database_url: str, statement: str
) -> None:
    xp.execute(text(statement))
    try:
        assert any("exports_purge_expired" in problem for problem in check_queue(xp))
        found = await startup_problems(app_database_url)
        assert DRIFTED in found
        assert "exports_purge_expired" in found
    finally:
        run_script(reinstall, EXPORTS_SCRIPT.read_text(encoding="utf-8"))
    assert check_queue(xp) == []
    assert await startup_problems(app_database_url) == ""


ROLE_DRIFT = [
    f"ALTER ROLE {ROLE} LOGIN",
    f"ALTER ROLE {ROLE} NOBYPASSRLS",
    f"ALTER ROLE {ROLE} INHERIT",
    f"ALTER ROLE {ROLE} SET work_mem = '64MB'",
    f"GRANT pg_read_all_data TO {ROLE}",
    f"GRANT {ROLE} TO ownvitals_owner",
    f"GRANT SELECT ON public.users TO {ROLE}",
    f"GRANT SELECT (bytes) ON public.export_chunks TO {ROLE}",
    f"GRANT SELECT (sha256) ON public.exports TO {ROLE}",
    f"GRANT UPDATE (download_count) ON public.exports TO {ROLE}",
    f"GRANT SELECT (state) ON public.jobs TO {ROLE}",
    # The ADR's purge writes no audit row: any privilege on auth_events is drift.
    f"GRANT INSERT (user_id, kind, detail) ON public.auth_events TO {ROLE}",
    f"GRANT SELECT (detail) ON public.auth_events TO {ROLE}",
    f"GRANT DELETE ON public.auth_events TO {ROLE}",
    f"GRANT UPDATE (failure) ON public.exports TO {ROLE}",
    f"GRANT TRUNCATE ON public.exports TO {ROLE}",
    f"REVOKE DELETE ON public.export_chunks FROM {ROLE}",
    f"REVOKE UPDATE (state) ON public.exports FROM {ROLE}",
    f"GRANT CREATE ON SCHEMA public TO {ROLE}",
]


@pytest.mark.parametrize("statement", ROLE_DRIFT)
def test_guard_2_finds_a_drifted_role_and_the_script_repairs_it(
    xp: Connection, reinstall: str, statement: str
) -> None:
    xp.execute(text(statement))
    try:
        assert check_roles(xp) != []  # the script then revokes it again
    finally:
        run_script(reinstall, EXPORTS_SCRIPT.read_text(encoding="utf-8"))
    assert check_roles(xp) == []


async def test_the_python_layer_calls_again_while_there_is_work(
    xp: Connection, jenv: JobsEnv
) -> None:
    owner = uuid.uuid4()
    for _ in range(150):
        plant(xp, owner, "ready", expires="-1 minute", chunks=0)
    live = plant(xp, owner, "ready", expires="1 hour", chunks=1)
    assert await purge_expired_exports(jenv.factory) == 150
    assert await purge_expired_exports(jenv.factory) == 0
    assert await purge_expired_exports(jenv.factory, batches=1) == 0
    assert states(xp)[live] == ("ready", None, 1)


@pytest.mark.parametrize("batches", [0, 101, True, 1.0])
async def test_the_python_layer_refuses_a_batch_count_out_of_bounds(
    xp: Connection, jenv: JobsEnv, batches: Any
) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        await purge_expired_exports(jenv.factory, batches=batches)


async def test_nothing_is_called_when_60_exports_sql_is_not_installed(
    xp: Connection, jenv: JobsEnv, queue_schema: str
) -> None:
    kept = plant(xp, uuid.uuid4(), "ready", expires="-1 minute")
    drop_exports_functions(queue_schema)
    try:
        assert await export_purge.purge_once(jenv.factory) is None
        assert await purge_expired_exports(jenv.factory) == 0
        assert states(xp)[kept] == ("ready", None, 2)
    finally:
        run_script(queue_schema, EXPORTS_SCRIPT.read_text(encoding="utf-8"))
    assert await purge_expired_exports(jenv.factory) == 3


async def test_the_worker_s_purge_tick_runs_it_and_logs_the_count_only(
    xp: Connection, jenv: JobsEnv, caplog: pytest.LogCaptureFixture
) -> None:
    gone = plant(xp, uuid.uuid4(), "ready", expires="-1 minute")
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        await upkeep.purge(jenv.factory, jenv.settings)
    assert states(xp)[gone] == ("expired", None, 0)
    counted = [r for r in caplog.records if getattr(r, "step", None) == "export purge"]
    assert [getattr(r, "count", None) for r in counted] == [3]
    assert str(gone) not in caplog.text
    assert gone.hex not in caplog.text


async def test_a_failing_export_purge_does_not_stop_the_tick(
    xp: Connection,
    jenv: JobsEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*_: object) -> int:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(upkeep, "purge_expired_exports", broken)
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        await upkeep.purge(jenv.factory, jenv.settings)
    failed = [r for r in caplog.records if r.getMessage() == "job maintenance failed"]
    assert [(getattr(r, "step", None), getattr(r, "error_type", None)) for r in failed] == [
        ("export purge", "RuntimeError")
    ]
