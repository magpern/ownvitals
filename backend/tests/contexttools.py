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
PARTITIONED: Final = ("sleep_series", "intraday", "observations")
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
    # The derived tables (#181).
    "observations": "INSERT INTO public.observations (owner_id, metric_key, ts, source_ts, "
    "source_tz, value, source_value_raw, source_unit, conversion, algo_version, input_revision, "
    "input_fingerprint) VALUES (:owner, :tag, :ts, :ts, 'Etc/UTC', 0, 0, 0, 'synthetic', "
    "'synthetic/1', 0, :hash) RETURNING id",
    "metric_daily": "INSERT INTO public.metric_daily (owner_id, metric_key, day, method, value, "
    "mean, min, max, mad, n, source_groups, algo_version, input_revision, input_fingerprint) "
    "VALUES (:owner, :tag, :day, 'median', 0, 0, 0, 0, 0, 1, ARRAY[:n]::bigint[], "
    "'synthetic/1', 0, :hash) RETURNING id",
    "findings": "INSERT INTO public.findings (owner_id, rule_id, rule_version, tier, severity, "
    "confidence, origin, subject, window_start, window_end, input_revision, input_fingerprint) "
    "VALUES (:owner, 'C1', '1', 'trend', 'attention', 'low', 'live', :tag, :day, :day, 0, "
    ":hash) RETURNING id",
    "scores": "INSERT INTO public.scores (owner_id, kind, day, value, components, algo_version, "
    "input_revision, input_fingerprint) VALUES (:owner, 'body_path:' || :tag, :day, 0, "
    "CAST('{\"status\": \"computed\"}' AS jsonb), 'synthetic/1', 0, :hash) RETURNING id",
    "user_profiles": "INSERT INTO public.user_profiles (owner_id) VALUES (:owner) RETURNING id",
    # The mail outbox (#194). A sent row, so the one-pending-per-kind index never collides.
    "mail_outbox": "INSERT INTO public.mail_outbox (owner_id, kind, route, state) "
    "VALUES (:owner, 'findings_waiting', 'findings', 'sent') RETURNING id",
    # The AI cost cap (#205): one month row per planting (n) and a settled reservation.
    "ai_spend": "INSERT INTO public.ai_spend (owner_id, month) VALUES "
    "(:owner, CAST(DATE '2000-01-01' + make_interval(months => :n) AS date)) RETURNING id",
    # The notification holds (#195): an ended hold of a synthetic finding id.
    "notify_holds": "INSERT INTO public.notify_holds (owner_id, finding_id, rule_id, started_at, "
    "revision_at, revision_seen, ended_at, reason, outcome) VALUES (:owner, gen_random_uuid(), "
    "'E4', :ts, :ts, :ts, :ts, 'timeout', 'sent_not_evaluated') RETURNING id",
    # AI consent and history (#206): a withdrawn consent (so the one-live-per-owner index never
    # collides), a conversation, and a message in the owner's own conversation.
    "ai_consents": "INSERT INTO public.ai_consents (owner_id, consent_version, locale, "
    "text_sha256, given_at, withdrawn_at) VALUES (:owner, 1, 'en', :hash, :ts, :ts) RETURNING id",
    "ai_conversations": "INSERT INTO public.ai_conversations (owner_id, created_at, updated_at) "
    "VALUES (:owner, :ts, :ts) RETURNING id",
    "ai_messages": "INSERT INTO public.ai_messages (owner_id, conversation_id, role, content) "
    "VALUES (:owner, (SELECT c.id FROM public.ai_conversations c WHERE c.owner_id = :owner "
    "ORDER BY c.created_at, c.id LIMIT 1), 'user', 'synthetic_ctx_message') RETURNING id",
    "ai_reservations": "INSERT INTO public.ai_reservations (owner_id, month, amount, actual, "
    "state, job_id, lease_version, ended_at) VALUES (:owner, DATE '2026-01-01', :n, :n, "
    "'settled', :job, 0, :ts) RETURNING id",
    # The AI audit log (#207): a failed call's redacted row (codes, counts, fingerprints).
    "ai_audit": "INSERT INTO public.ai_audit (owner_id, job_id, kind, model, prompt_version, "
    "toolset_version, cost_microusd, reservation_id, latency_ms, attempts, status, error, "
    "settlement, request_hmac) VALUES (:owner, :job, 'chat', 'synthetic-model', '1', '1', :n, "
    "gen_random_uuid(), 0, 1, 'failed', 'not_sent', 'released', :hash) RETURNING id",
}
# Tables with one row per owner (a unique key on ``owner_id`` alone): the own-insert control
# first removes the context's own row, in the same rolled-back savepoint, or the planted row
# would make it fail on the key. The foreign insert stays a plain insert in its minimal shape:
# with the policy it is refused (42501) before the key is checked; with a relaxed ``WITH CHECK``
# it reaches the key (23505) or goes through, and either is not ``REFUSED``.
CLEAR_BEFORE_OWN_INSERT: Final[Mapping[str, str]] = {
    "user_profiles": "DELETE FROM public.user_profiles WHERE owner_id = :owner",
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


def without_returning(statement: str) -> str:
    """``statement`` without its ``RETURNING`` clause. The probe's inserts must not return
    anything: a returned row has to pass the read policy too, which would refuse a cross-user
    insert that an allow-all ``FOR INSERT`` policy lets through, and hide that hole."""
    head, returning, _ = statement.rpartition(" RETURNING ")
    if not returning:
        raise AssertionError("a ROWS statement ends in RETURNING id")
    return head


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


async def attempt(
    db: AsyncSession, statement: str, params: Mapping[str, Any], *, before: str | None = None
) -> Outcome:
    """Run one write in a savepoint that is always rolled back: the rows it touched, or the
    SQLSTATE of the error that refused it. ``before`` runs first in the same savepoint (its
    rows are not counted)."""
    rows = 0
    try:
        async with db.begin_nested():
            if before is not None:
                await db.execute(text(before), dict(params))
            result = await db.execute(text(statement), dict(params))
            rows = int(result.rowcount)  # pyright: ignore[reportAttributeAccessIssue]
            raise _Undo
    except _Undo:
        return rows
    except DBAPIError as exc:
        return str(getattr(exc.orig, "sqlstate", None) or "unknown")


_RESTRICTED = text(
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid = to_regclass(:t) "
    "AND NOT p.polpermissive AND p.polcmd IN (:cmd, '*'))"
)
_PRIVILEGE = text("SELECT has_table_privilege(:t, :privilege)")


async def _expected_own(db: AsyncSession, name: str, privilege: str, cmd: str) -> Outcome | None:
    """What a write of the context's own rows must do: ``REFUSED`` without the privilege, ``0``
    under a restrictive policy for the command (``job_events`` is append-only), else ``None``
    (it touches exactly the rows the context sees)."""
    params = {"t": f"public.{name}"}
    if not (await db.execute(_PRIVILEGE, {**params, "privilege": privilege})).scalar_one():
        return REFUSED
    if (await db.execute(_RESTRICTED, {**params, "cmd": cmd})).scalar_one():
        return 0
    return None


async def probe(
    db: AsyncSession, me: Owner, other: Owner, tables: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """For each owned table, in the context's own transaction ``db``: what it reads, and what
    each write does when tried in its **minimal shape**.

    No write has a ``WHERE`` or a ``RETURNING``: either would make the read policy apply as
    well (Postgres checks it for rows a statement reads or returns) and so hide a hole in a
    write policy. Each command is tried once against the other user and, as the positive
    control, once against the context's own rows:

    - ``seen`` / ``visible``: the owners and the number of rows a plain ``SELECT`` returns;
    - ``foreign_insert``: a row for the other user; ``own_insert``: one for the context's user;
    - ``move``: ``UPDATE t SET owner_id = :other``, every row it may update handed to the
      other user (an ``UPDATE`` policy whose ``WITH CHECK`` is wider than the owner shows);
    - ``blind_update``: ``UPDATE t SET owner_id = :me``, changes nothing it may update, so it
      must touch exactly the rows the context sees (wider ``USING`` shows as more rows);
    - ``blind_delete``: ``DELETE FROM t``, likewise;
    - ``truncate``: ``TRUNCATE t``, which ignores row security, must be refused.

    Every write runs in a savepoint that is rolled back."""
    report: dict[str, dict[str, Any]] = {}
    for table in sorted(tables):
        if table not in ROWS:
            raise Uncovered(f"owned table {table!r} has no row in contexttools.ROWS")
        name = checked(table)
        insert = without_returning(ROWS[table])
        seen = await db.execute(
            text(f"SELECT DISTINCT CAST(owner_id AS text) FROM public.{name} ORDER BY 1")  # noqa: S608
        )
        visible = await db.execute(text(f"SELECT count(*) FROM public.{name}"))  # noqa: S608
        update = f"UPDATE public.{name} SET owner_id = :owner"  # noqa: S608
        report[table] = {
            "seen": [str(v) for v in seen.scalars()],
            "visible": int(visible.scalar_one()),
            "expect_insert": await _expected_own(db, name, "INSERT", "a"),
            "expect_update": await _expected_own(db, name, "UPDATE", "w"),
            "expect_delete": await _expected_own(db, name, "DELETE", "d"),
            "foreign_insert": await attempt(db, insert, bind(insert, other.params(3))),
            "move": await attempt(db, update, {"owner": other.user}),
            "blind_update": await attempt(db, update, {"owner": me.user}),
            "blind_delete": await attempt(db, f"DELETE FROM public.{name}", {}),  # noqa: S608
            "truncate": await attempt(db, f"TRUNCATE public.{name}", {}),
            "own_insert": await attempt(
                db, insert, bind(insert, me.params(2)), before=CLEAR_BEFORE_OWN_INSERT.get(table)
            ),
        }
    return report


def assert_isolated(
    report: Mapping[str, Mapping[str, Any]], me: Owner, tables: Iterable[str]
) -> None:
    """A context of ``me``: it reads only its own rows; nothing it writes reaches the other
    user (the insert and the move are refused) and its blind writes touch exactly its own
    rows; its own insert goes through wherever the app may insert."""
    assert set(report) == set(tables), "the probe did not cover every owned table"
    for table, row in report.items():
        visible = row["visible"]
        assert row["seen"] == [str(me.user)], (table, "read", row["seen"])
        assert row["foreign_insert"] == REFUSED, (table, "insert", row["foreign_insert"])
        assert row["move"] in (0, REFUSED), (table, "move", row["move"])
        for key, expect in (("blind_update", "expect_update"), ("blind_delete", "expect_delete")):
            wanted = visible if row[expect] is None else row[expect]
            assert row[key] == wanted, (table, key, row[key], wanted)
        assert row["truncate"] == REFUSED, (table, "truncate", row["truncate"])
        wanted = 1 if row["expect_insert"] is None else row["expect_insert"]
        assert row["own_insert"] == wanted, (table, "own insert", row["own_insert"])


def assert_no_context(report: Mapping[str, Mapping[str, Any]], tables: Iterable[str]) -> None:
    """A session without a user (the webhook receiver's, the scheduler's, maintenance): no owned
    row is visible and no owned row can be written, for anyone."""
    assert set(report) == set(tables), "the probe did not cover every owned table"
    for table, row in report.items():
        assert row["seen"] == [], (table, "read")
        assert row["visible"] == 0, (table, "count")
        for key in ("move", "blind_update", "blind_delete"):
            assert row[key] in (0, REFUSED), (table, key, row[key])
        for key in ("foreign_insert", "own_insert", "truncate"):
            assert row[key] == REFUSED, (table, key, row[key])


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
    columns it may read, a read of the owner and of every column, and every write in its
    minimal shape (no ``WHERE``, no ``RETURNING``, so no read grant is needed and only a write
    grant decides): an insert of each user's row, an unconditional ``DELETE`` and
    ``TRUNCATE``, and for every column an ``UPDATE`` that sets it to ``DEFAULT`` and an
    ``INSERT`` of it alone, so a column-level grant on any of them shows. Every write runs in a
    savepoint that is rolled back."""
    report: dict[str, dict[str, Any]] = {}
    for table in sorted(tables):
        if table not in ROWS:
            raise Uncovered(f"owned table {table!r} has no row in contexttools.ROWS")
        name = checked(table)
        insert = without_returning(ROWS[table])
        readable = await db.execute(_READABLE, {"t": f"public.{name}"})
        columns = [
            checked(str(c)) for c in (await db.execute(_COLUMNS, {"t": f"public.{name}"})).scalars()
        ]
        writes: list[tuple[str, Outcome]] = []
        for n, owner in enumerate(owners, start=4):
            writes.append(("insert", await attempt(db, insert, bind(insert, owner.params(n)))))
        writes.append(("delete", await attempt(db, f"DELETE FROM public.{name}", {})))  # noqa: S608
        writes.append(("truncate", await attempt(db, f"TRUNCATE public.{name}", {})))
        for column in columns:
            update = f'UPDATE public.{name} SET "{column}" = DEFAULT'  # noqa: S608
            insert_one = f'INSERT INTO public.{name} ("{column}") VALUES (DEFAULT)'  # noqa: S608
            writes.append((f"update {column}", await attempt(db, update, {})))
            writes.append((f"insert {column}", await attempt(db, insert_one, {})))
        report[table] = {
            "readable": [str(v) for v in readable.scalars()],
            "read_owner": await attempt(db, f"SELECT owner_id FROM public.{name}", {}),  # noqa: S608
            "read_all": await attempt(db, f"SELECT * FROM public.{name}", {}),  # noqa: S608
            "writes": writes,
        }
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
        for label, outcome in row["writes"]:
            assert outcome in write_refusals, (table, label, outcome)
