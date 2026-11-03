"""RLS in every execution context (ADR 0002 sections 4 and 7, guard "ctx"; issue #15).

Two synthetic users each get one row in **every owned table** (``ROWS``); then each execution
context of ADR 0002 section 4 runs the same probe the way that context reaches owned rows, and
the test module of that context asserts what the probe may and may not do:

- ``test_isolation_contexts_web.py``: a web request, signed in through ``ownvitals.auth``;
- ``test_isolation_contexts_worker.py``: a worker job a user created, issued by the job issuer;
- ``test_isolation_contexts_scheduler.py``: a job the scheduler enqueued (``queue_enqueue``);
- ``test_isolation_contexts_webhook.py``: a job the webhook receiver's ingest function created;
- ``test_isolation_contexts_maintenance.py``: the reaper and the per-owner purge function;
- ``test_isolation_contexts_admin.py``: an admin route on ``ownvitals_maint``.

``probe`` runs in one transaction of the context, for each owned table: which owners it sees,
an update, a delete and an insert aimed at the other user's rows (the "cross-user write"), a move
of its own rows to the other user, and an insert of its own (the positive control that shows the
cross-user insert is refused for its owner, not for its columns). Every write runs in a savepoint
that is rolled back, so nothing the probe does survives, and ``snapshot`` shows each user's rows
unchanged afterwards.

**The enumeration guard.** ``owned_tables`` reads the catalog: every table in ``public`` with an
``owner_id`` column (partitioned parents included, their partitions not). ``probe`` raises
``Uncovered`` for a table without a ``ROWS`` entry, every context asserts its report covers the
catalog, and ``test_isolation_contexts_guard.py`` fails while ``ROWS`` and the catalog differ.
A new owned table therefore fails every context until its synthetic row is added here.

Fixtures are synthetic: labelled filler bytes, fixed small integers, no measurement values.
"""

import asyncio
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobtools import ensure_user
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.context import UserContext
from ownvitals.jobs import Complete, JobKind, JobRun, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute

Factory = async_sessionmaker[AsyncSession]

# Insufficient privilege: what Postgres answers for both a row-level security violation and a
# missing grant.
REFUSED: Final = "42501"
# A write in a read-only transaction (the admin area's default).
READ_ONLY: Final = "25006"
# Labelled filler, 32 bytes as the hash columns require. Not a digest of anything.
SYNTHETIC_HASH: Final = b"\x5a" * 32
TABLE_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,62}")
PARTITIONED: Final = ("sleep_series", "intraday")
CONTEXTS: Final = ("web", "worker", "scheduler", "webhook", "maintenance", "admin")
LEASE_SECONDS: Final = 60

OWNED_TABLES_SQL: Final = """
SELECT c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute a
    WHERE a.attrelid = c.oid AND a.attname = 'owner_id' AND a.attnum > 0 AND NOT a.attisdropped
  )
ORDER BY 1
"""

# Owned table -> the INSERT that plants one synthetic row for ``:owner`` (in this order: a row's
# parents come first). Parameters: ``owner``, ``connection`` (the owner's Withings connection),
# ``job`` and ``group`` (the owner's planted ``jobs`` and ``measure_groups`` rows), ``n`` and
# ``tag`` (distinct per planting, so no unique key collides), ``day``, ``ts`` and ``hash``.
ROWS: Final[Mapping[str, str]] = {
    "connections": "INSERT INTO public.connections (owner_id, provider, status) "
    "VALUES (:owner, 'test', 'disconnected') RETURNING id",
    "jobs": "INSERT INTO public.jobs (owner_id, kind, state, input, idempotency_key, "
    "request_hash) VALUES (:owner, 'test_kind', 'queued', CAST('{}' AS jsonb), :tag, :hash) "
    "RETURNING id",
    "job_events": "INSERT INTO public.job_events (owner_id, job_id, seq, kind, payload, "
    "lease_version) VALUES (:owner, :job, :n, 'queued', CAST('{}' AS jsonb), 0) RETURNING id",
    "measure_groups": "INSERT INTO public.measure_groups (owner_id, connection_id, grpid, "
    "measured_at, source_tz, payload_hash) VALUES (:owner, :connection, :n, :ts, 'Etc/UTC', "
    ":hash) RETURNING id",
    "measures": "INSERT INTO public.measures (owner_id, measure_group_id, type, value_raw, unit) "
    "VALUES (:owner, :group, :n, 0, 0) RETURNING id",
    "devices": "INSERT INTO public.devices (owner_id, connection_id, hash_deviceid) "
    "VALUES (:owner, :connection, :tag) RETURNING id",
    "ecg_recordings": "INSERT INTO public.ecg_recordings (owner_id, connection_id, signalid, "
    "recorded_at, source_tz) VALUES (:owner, :connection, :n, :ts, 'Etc/UTC') RETURNING id",
    "sleep_summaries": "INSERT INTO public.sleep_summaries (owner_id, connection_id, day, "
    "start_at, end_at, source_tz, payload_hash) VALUES (:owner, :connection, :day, :ts, :ts, "
    "'Etc/UTC', :hash) RETURNING id",
    "activity_daily": "INSERT INTO public.activity_daily (owner_id, connection_id, day, start_at, "
    "source_tz, payload_hash) VALUES (:owner, :connection, :day, :ts, 'Etc/UTC', :hash) "
    "RETURNING id",
    "workouts": "INSERT INTO public.workouts (owner_id, connection_id, workout_id, start_at, "
    "end_at, source_tz, payload_hash) VALUES (:owner, :connection, :n, :ts, :ts, 'Etc/UTC', "
    ":hash) RETURNING id",
    "sync_cursors": "INSERT INTO public.sync_cursors (owner_id, connection_id, stream) "
    "VALUES (:owner, :connection, :tag) RETURNING id",
    "data_revisions": "INSERT INTO public.data_revisions (owner_id, stream) "
    "VALUES (:owner, :tag) RETURNING id",
    "sleep_series": "INSERT INTO public.sleep_series (owner_id, ts, source_tz, connection_id, "
    "series, value_raw, unit) VALUES (:owner, :ts, 'Etc/UTC', :connection, :tag, 0, 0) "
    "RETURNING id",
    "intraday": "INSERT INTO public.intraday (owner_id, ts, source_tz, connection_id, metric, "
    "value_raw, unit) VALUES (:owner, :ts, 'Etc/UTC', :connection, :tag, 0, 0) RETURNING id",
}
_PARAM = re.compile(r"(?<![:\w]):([a-z_]+)")
_WITHINGS_CONNECTION = text(
    "INSERT INTO public.connections (owner_id, provider, status, public) "
    "VALUES (:owner, 'withings', 'needs_reconnect', CAST(:public AS jsonb)) RETURNING id"
)


class Uncovered(AssertionError):
    """An owned table has no synthetic row in ``ROWS``, so no context test covers it."""


def checked(table: str) -> str:
    """A table name that is safe to put in SQL text (it comes from the catalog or ``ROWS``)."""
    if TABLE_NAME.fullmatch(table) is None:
        raise AssertionError(f"not a plain table name: {table!r}")
    return table


def owned_tables(url: str) -> frozenset[str]:
    """Every owned table of the migrated schema, from the catalog (any role may read it)."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            names = connection.execute(text(OWNED_TABLES_SQL)).scalars()
            return frozenset(str(name) for name in names)
    finally:
        engine.dispose()


def uncovered(found: Iterable[str]) -> tuple[list[str], list[str]]:
    """``(missing, stale)``: owned tables without a ``ROWS`` entry, and entries for tables that
    are not owned (or no longer exist)."""
    found = set(found)
    return sorted(found - set(ROWS)), sorted(set(ROWS) - found)


def series_month(now: datetime | None = None) -> date:
    """The month the series rows go in: the current one (UTC), which
    ``ensure_month_partitions`` may create."""
    now = now or datetime.now(UTC)
    return date(now.year, now.month, 1)


@dataclass(frozen=True)
class Owner:
    """One synthetic user and the ids its planted rows point at."""

    user: UUID
    connection: UUID  # the live Withings connection
    job: UUID  # the planted ``jobs`` row
    group: UUID  # the planted ``measure_groups`` row

    def params(self, n: int) -> dict[str, Any]:
        start = datetime.combine(series_month(), datetime.min.time(), UTC)
        return {
            "owner": self.user,
            "connection": self.connection,
            "job": self.job,
            "group": self.group,
            "n": n,
            "tag": f"synthetic_ctx_key_{n}",
            "day": date(2026, 1, n),
            "ts": start + timedelta(days=1, minutes=n),
            "hash": SYNTHETIC_HASH,
        }

    def as_json(self) -> dict[str, str]:
        return {k: str(v) for k, v in vars(self).items()}

    @classmethod
    def from_json(cls, data: Mapping[str, str]) -> "Owner":
        return cls(**{k: UUID(data[k]) for k in ("user", "connection", "job", "group")})


def bind(statement: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters ``statement`` names, from ``params``."""
    return {name: params[name] for name in set(_PARAM.findall(statement))}


@contextmanager
def superuser(url: str) -> Iterator[Connection]:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def ensure_partitions(url: str) -> None:
    """The current month's partitions, through the reviewed function (ADR 0002 section 2)."""
    month = series_month()
    with superuser(url) as conn:
        for table in PARTITIONED:
            conn.execute(
                text("SELECT public.ensure_month_partitions(:t, :m, :m)"), {"t": table, "m": month}
            )


def plant(
    url: str, user: UUID, *, app_id: int | None = None, withings_userid: int | None = None
) -> Owner:
    """As the superuser: the user's account (when missing), a live Withings connection (linked
    to ``app_id``/``withings_userid`` when given) and one synthetic row in every ``ROWS``
    table."""
    ensure_partitions(url)
    public = "{}"
    if app_id is not None and withings_userid is not None:
        public = (
            f'{{"app_id": {int(app_id)}, "withings_userid": {int(withings_userid)}, '
            '"scopes": ["user.metrics"]}'
        )
    with superuser(url) as conn:
        ensure_user(conn, user)
        connection = conn.execute(_WITHINGS_CONNECTION, {"owner": user, "public": public})
        partial = Owner(user, connection.scalar_one(), user, user)
        ids: dict[str, UUID] = {}
        for table, statement in ROWS.items():
            # ``jobs`` and ``measure_groups`` come before the rows that point at them.
            job, group = ids.get("jobs", user), ids.get("measure_groups", user)
            params = Owner(user, partial.connection, job, group).params(1)
            ids[table] = conn.execute(text(statement), bind(statement, params)).scalar_one()
    return Owner(user, partial.connection, ids["jobs"], ids["measure_groups"])


def snapshot(url: str, owners: Iterable[Owner]) -> dict[tuple[str, str], int]:
    """Rows per (owned table, owner), as the superuser sees them."""
    counts: dict[tuple[str, str], int] = {}
    with superuser(url) as conn:
        for owner in owners:
            for table in ROWS:
                counted = conn.execute(
                    text(f"SELECT count(*) FROM public.{checked(table)} WHERE owner_id = :o"),  # noqa: S608
                    {"o": owner.user},
                ).scalar_one()
                counts[(table, str(owner.user))] = int(counted)
    return counts


def assert_unchanged(
    before: Mapping[tuple[str, str], int],
    after: Mapping[tuple[str, str], int],
    *,
    changed: Iterable[tuple[str, Owner]] = (),
) -> None:
    """Every (table, owner) count of ``before`` is the same in ``after``, except the ``changed``
    ones (a probe job's own row and events)."""
    skip = {(table, str(owner.user)) for table, owner in changed}
    assert set(before) == set(after)
    for key, count in before.items():
        if key not in skip:
            assert after[key] == count, key


# --- the probe -----------------------------------------------------------------------------------


class _Undo(Exception):
    """Rolls a probe's savepoint back after a write that went through."""


Outcome = int | str  # rows affected, or the SQLSTATE that refused it


async def attempt(db: AsyncSession, statement: str, params: Mapping[str, Any]) -> Outcome:
    """Run one write in a savepoint that is always rolled back: the rows it touched, or the
    SQLSTATE of the error that refused it."""
    rows = 0
    try:
        async with db.begin_nested():
            result = await db.execute(text(statement), dict(params))
            rows = int(result.rowcount)  # pyright: ignore[reportAttributeAccessIssue]
            raise _Undo
    except _Undo:
        return rows
    except DBAPIError as exc:
        return str(getattr(exc.orig, "sqlstate", None) or "unknown")


async def probe(
    db: AsyncSession, me: Owner, other: Owner, tables: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """For each owned table, in the context's own transaction ``db``: the owners it sees, how
    many rows it sees, and what each write aimed at ``other`` (and the control write of ``me``)
    did.

    ``foreign_update`` and ``foreign_delete`` name the other owner in their ``WHERE``, which
    makes the read policy apply too. ``blind_update`` and ``blind_delete`` read no column (no
    ``WHERE``, a constant ``SET``), so only the write policies decide which rows they touch: an
    extra permissive ``FOR UPDATE`` or ``FOR DELETE`` policy shows up there as more rows than
    the context can see."""
    report: dict[str, dict[str, Any]] = {}
    for table in sorted(tables):
        if table not in ROWS:
            raise Uncovered(f"owned table {table!r} has no row in contexttools.ROWS")
        name = checked(table)
        insert = ROWS[table]
        seen = await db.execute(
            text(f"SELECT DISTINCT CAST(owner_id AS text) FROM public.{name} ORDER BY 1")  # noqa: S608
        )
        privilege = await db.execute(
            text("SELECT has_table_privilege(:t, 'INSERT')"), {"t": f"public.{name}"}
        )
        visible = await db.execute(text(f"SELECT count(*) FROM public.{name}"))  # noqa: S608
        report[table] = {
            "seen": [str(v) for v in seen.scalars()],
            "visible": int(visible.scalar_one()),
            "insert_privilege": bool(privilege.scalar_one()),
            "foreign_update": await attempt(
                db,
                f"UPDATE public.{name} SET owner_id = owner_id WHERE owner_id = :other",  # noqa: S608
                {"other": other.user},
            ),
            "foreign_delete": await attempt(
                db,
                f"DELETE FROM public.{name} WHERE owner_id = :other",  # noqa: S608
                {"other": other.user},
            ),
            "foreign_insert": await attempt(db, insert, bind(insert, other.params(3))),
            "own_move": await attempt(
                db,
                f"UPDATE public.{name} SET owner_id = :other WHERE owner_id = :me",  # noqa: S608
                {"other": other.user, "me": me.user},
            ),
            "blind_update": await attempt(
                db,
                f"UPDATE public.{name} SET owner_id = :me",  # noqa: S608
                {"me": me.user},
            ),
            "blind_delete": await attempt(db, f"DELETE FROM public.{name}", {}),  # noqa: S608
            "own_insert": await attempt(db, insert, bind(insert, me.params(2))),
        }
    return report


def _within(outcome: Outcome, visible: int) -> bool:
    """A write that read no column was refused or touched at most the rows the context sees."""
    return outcome == REFUSED or (isinstance(outcome, int) and 0 <= outcome <= visible)


def assert_isolated(
    report: Mapping[str, Mapping[str, Any]], me: Owner, tables: Iterable[str]
) -> None:
    """A context of ``me``: it sees only its own rows and every write at the other user's rows
    is refused or touches nothing, while its own insert goes through wherever the app may
    insert."""
    assert set(report) == set(tables), "the probe did not cover every owned table"
    for table, row in report.items():
        assert row["seen"] == [str(me.user)], (table, "read")
        assert row["foreign_update"] in (0, REFUSED), (table, "update", row["foreign_update"])
        assert row["foreign_delete"] in (0, REFUSED), (table, "delete", row["foreign_delete"])
        assert row["foreign_insert"] == REFUSED, (table, "insert", row["foreign_insert"])
        assert row["own_move"] in (0, REFUSED), (table, "move", row["own_move"])
        for key in ("blind_update", "blind_delete"):
            assert _within(row[key], row["visible"]), (table, key, row[key], row["visible"])
        expected = 1 if row["insert_privilege"] else REFUSED
        assert row["own_insert"] == expected, (table, "own insert", row["own_insert"])


def assert_no_context(report: Mapping[str, Mapping[str, Any]], tables: Iterable[str]) -> None:
    """A session without a user (the webhook receiver's, the scheduler's): no owned row is
    visible and no owned row can be written, for anyone."""
    assert set(report) == set(tables), "the probe did not cover every owned table"
    for table, row in report.items():
        assert row["seen"] == [], (table, "read")
        assert row["visible"] == 0, (table, "count")
        for key in ("foreign_update", "foreign_delete", "own_move", "blind_update", "blind_delete"):
            assert row[key] in (0, REFUSED), (table, key, row[key])
        assert row["foreign_insert"] == REFUSED, (table, "insert", row["foreign_insert"])
        assert row["own_insert"] == REFUSED, (table, "own insert", row["own_insert"])


# --- jobs (worker, scheduler and webhook contexts) -----------------------------------------------


class Target(BaseModel):
    """A probe job's input: the other user's ids, as a hostile job input would name them."""

    model_config = ConfigDict(extra="forbid")
    user: UUID
    connection: UUID
    job: UUID
    group: UUID


@dataclass
class JobProbe:
    """What the probe kind saw while it ran: the issued context and the probe's report."""

    ctx: UserContext | None = None
    report: dict[str, dict[str, Any]] | None = None


async def run_probe_job(
    factory: Factory,
    kind: str,
    input_model: type[BaseModel],
    owners: Iterable[Owner],
    tables: Iterable[str],
) -> tuple[Execution, JobProbe]:
    """Claim the next ``kind`` job and run it as the worker does (``runner.execute``: the job
    issuer with the worker's account directory ``ACCOUNTS``, the fence). The handler probes in
    its fenced transaction as the issued context's user; the other user is the job's ``Target``
    input when it has one, otherwise the other of the two ``owners``."""
    by_user = {owner.user: owner for owner in owners}
    tables = frozenset(tables)
    seen = JobProbe()

    async def run(job_run: JobRun) -> Complete:
        seen.ctx = job_run.ctx
        me = by_user[job_run.ctx.user_id]
        if isinstance(job_run.input, Target):
            other = Owner(**job_run.input.model_dump())
        else:
            (other,) = (owner for user, owner in by_user.items() if user != me.user)
        async with job_run.fenced() as fs:
            seen.report = await probe(fs.db, me, other, tables)
        return Complete()

    registry = KindRegistry()
    registry.register(JobKind(kind, input_model, run))
    claimed = await queue.claim(factory, "ctx-probe-worker", LEASE_SECONDS, [kind])
    assert claimed is not None, "nothing to claim"
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=LEASE_SECONDS, accounts=ACCOUNTS)
    await execute(execution, factory, registry, config, asyncio.Event())
    return execution, seen


def assert_job_isolated(
    execution: Execution, seen: JobProbe, me: Owner, tables: Iterable[str]
) -> None:
    """The job ran to completion as ``me`` alone: a ``role="user"`` context of ``me`` whose
    connection refs are ``me``'s, and a probe that stayed inside ``me``'s rows."""
    assert execution.result == "completed", execution.result
    assert seen.ctx is not None
    assert seen.report is not None
    assert (seen.ctx.user_id, seen.ctx.role) == (me.user, "user")
    assert seen.ctx.connections == frozenset({("withings", me.connection)})
    assert_isolated(seen.report, me, tables)


# --- admin (ownvitals_maint) ---------------------------------------------------------------------

# Owned tables whose status and metadata columns the admin area may read (ADR 0002 section 4);
# every other owned table holds health data, of which it may read nothing.
ADMIN_METADATA_TABLES: Final = frozenset({"connections", "jobs", "job_events"})
_COLUMNS = text(
    "SELECT a.attname FROM pg_catalog.pg_attribute a "
    "WHERE a.attrelid = to_regclass(:t) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1"
)
_READABLE = text(
    "SELECT a.attname FROM pg_catalog.pg_attribute a "
    "WHERE a.attrelid = to_regclass(:t) AND a.attnum > 0 AND NOT a.attisdropped "
    "AND has_column_privilege(a.attrelid, a.attnum, 'SELECT') ORDER BY 1"
)


async def admin_probe(
    db: AsyncSession, owners: Iterable[Owner], tables: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """For each owned table, in an admin transaction as ``ownvitals_maint`` (``BYPASSRLS``, so
    only its grants and the read-only default stand between it and every user's rows): the
    columns it may read, a read of the owner and of every column, an update, a delete and an
    insert aimed at each user's rows, and the writes that read no column, so only a write grant
    could let them through: an unconditional ``DELETE``, and for every column an ``UPDATE`` that
    sets it to ``DEFAULT`` and an ``INSERT`` of it alone (one per column, so a column-level grant
    on any of them shows)."""
    report: dict[str, dict[str, Any]] = {}
    for table in sorted(tables):
        if table not in ROWS:
            raise Uncovered(f"owned table {table!r} has no row in contexttools.ROWS")
        name = checked(table)
        readable = await db.execute(_READABLE, {"t": f"public.{name}"})
        columns = [
            checked(str(c)) for c in (await db.execute(_COLUMNS, {"t": f"public.{name}"})).scalars()
        ]
        row: dict[str, Any] = {
            "readable": [str(v) for v in readable.scalars()],
            "read_owner": await attempt(db, f"SELECT owner_id FROM public.{name}", {}),  # noqa: S608
            "read_all": await attempt(db, f"SELECT * FROM public.{name}", {}),  # noqa: S608
            "writes": [],
        }
        for n, owner in enumerate(owners, start=4):
            row["writes"] += [
                await attempt(
                    db,
                    f"UPDATE public.{name} SET owner_id = owner_id WHERE owner_id = :o",  # noqa: S608
                    {"o": owner.user},
                ),
                await attempt(
                    db,
                    f"DELETE FROM public.{name} WHERE owner_id = :o",  # noqa: S608
                    {"o": owner.user},
                ),
                await attempt(db, ROWS[table], bind(ROWS[table], owner.params(n))),
            ]
        row["writes"].append(await attempt(db, f"DELETE FROM public.{name}", {}))  # noqa: S608
        for column in columns:
            row["writes"] += [
                await attempt(db, f'UPDATE public.{name} SET "{column}" = DEFAULT', {}),  # noqa: S608
                await attempt(
                    db,
                    f'INSERT INTO public.{name} ("{column}") VALUES (DEFAULT)',  # noqa: S608
                    {},
                ),
            ]
        report[table] = row
    return report


def assert_admin_isolated(
    report: Mapping[str, Mapping[str, Any]], tables: Iterable[str], *, write: bool
) -> None:
    """The admin area reads no owner and no health column of any owned table, and writes no
    owned row: refused by its grants (``write``) or by its read-only transaction."""
    assert set(report) == set(tables), "the probe did not cover every owned table"
    write_refusals = (REFUSED,) if write else (REFUSED, READ_ONLY)
    for table, row in report.items():
        assert "owner_id" not in row["readable"], table
        if table not in ADMIN_METADATA_TABLES:
            assert row["readable"] == [], (table, row["readable"])
        assert row["read_owner"] == REFUSED, (table, row["read_owner"])
        assert row["read_all"] == REFUSED, (table, row["read_all"])
        assert row["writes"], table
        for outcome in row["writes"]:
            assert outcome in write_refusals, (table, row["writes"])
