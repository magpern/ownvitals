"""Account deletion revokes first, then cascades every owned row. Synthetic rows only."""

import re
import urllib.request
from uuid import UUID

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals.auth.purge import EXPLICIT_PURGE, delete_owner, finish_disconnect
from ownvitals.connections.providers.withings import store_grant
from ownvitals.egress import core
from ownvitals.withings.disconnect import DisconnectRefused
from test_withings_disconnect import _hits, _linked, _run, _scalar

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
_NAME = re.compile(r"[a-z_]+")
_PARTITIONS = (
    (
        "sleep_series_y2026m11",
        "sleep_series",
    ),
    (
        "intraday_y2026m11",
        "intraday",
    ),
    (
        "observations_y2026m11",
        "observations",
    ),
)
_OWNED = """
SELECT c.relname,
       EXISTS (
         SELECT 1 FROM pg_constraint f
         JOIN pg_attribute a ON a.attrelid = f.conrelid AND a.attnum = ANY (f.conkey)
         WHERE f.conrelid = c.oid AND f.contype = 'f' AND a.attname = 'owner_id'
           AND f.confrelid = 'public.users'::regclass AND f.confdeltype = 'c'
       ) AS cascades
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND EXISTS (
    SELECT 1 FROM pg_attribute a
    WHERE a.attrelid = c.oid AND a.attname = 'owner_id' AND a.attnum > 0
      AND NOT a.attisdropped
  )
ORDER BY 1
"""


@pytest.fixture(autouse=True)
def _web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture
async def owner(owner_database_url: str):
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _rows(url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql)).all())
    finally:
        engine.dispose()


def _partitions(url: str, present: bool) -> None:
    engine = create_engine(url)
    policy = (
        "CREATE POLICY owner_isolation ON public.{name} AS PERMISSIVE FOR ALL TO PUBLIC "
        "USING (owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid) "
        "WITH CHECK (owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid)"
    )
    try:
        with engine.begin() as conn:
            for name, parent in _PARTITIONS:
                conn.execute(text(f"DROP TABLE IF EXISTS public.{name}"))
                if present:
                    conn.execute(
                        text(
                            f"CREATE TABLE public.{name} PARTITION OF public.{parent} "
                            "FOR VALUES FROM ('2026-11-01') TO ('2026-12-01')"
                        )
                    )
                    conn.execute(text(f"ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY"))
                    conn.execute(text(f"ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY"))
                    conn.execute(text(policy.format(name=name)))
    finally:
        engine.dispose()


def _plant(url: str, owner_id: UUID, connection: UUID, tag: str) -> None:
    """One synthetic row in every owned table except the connection, which already exists."""
    digest = b"\x11" * 32
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            job = conn.execute(
                text(
                    "INSERT INTO jobs (owner_id, kind, state, input, idempotency_key, "
                    "request_hash) "
                    "VALUES (:o, 'withings_sync', 'queued', '{}', :k, :h) RETURNING id"
                ),
                {"o": owner_id, "k": f"synthetic-job-{tag}-0001", "h": digest},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO job_events (owner_id, job_id, seq, kind, payload, lease_version) "
                    "VALUES (:o, :j, 1, 'queued', '{}', 0)"
                ),
                {"o": owner_id, "j": job},
            )
            group = conn.execute(
                text(
                    "INSERT INTO measure_groups (owner_id, connection_id, grpid, measured_at, "
                    "source_tz, payload_hash) VALUES (:o, :c, 1, '2026-11-02 08:00:00+00', "
                    "'Etc/UTC', :h) RETURNING id"
                ),
                {"o": owner_id, "c": connection, "h": digest},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO measures (owner_id, measure_group_id, type, value_raw, unit) "
                    "VALUES (:o, :g, 1, 0, 0)"
                ),
                {"o": owner_id, "g": group},
            )
            conn.execute(
                text(
                    "INSERT INTO devices (owner_id, connection_id, hash_deviceid) "
                    "VALUES (:o, :c, 'synthetic-device')"
                ),
                {"o": owner_id, "c": connection},
            )
            conn.execute(
                text(
                    "INSERT INTO ecg_recordings (owner_id, connection_id, signalid, recorded_at, "
                    "source_tz) VALUES (:o, :c, 1, '2026-11-02 08:00:00+00', 'Etc/UTC')"
                ),
                {"o": owner_id, "c": connection},
            )
            conn.execute(
                text(
                    "INSERT INTO sleep_summaries (owner_id, connection_id, day, start_at, end_at, "
                    "source_tz, payload_hash) VALUES (:o, :c, '2026-11-02', "
                    "'2026-11-02 00:00:00+00', '2026-11-02 08:00:00+00', 'Etc/UTC', :h)"
                ),
                {"o": owner_id, "c": connection, "h": digest},
            )
            conn.execute(
                text(
                    "INSERT INTO activity_daily (owner_id, connection_id, day, start_at, "
                    "source_tz, payload_hash) VALUES (:o, :c, '2026-11-02', "
                    "'2026-11-02 00:00:00+00', 'Etc/UTC', :h)"
                ),
                {"o": owner_id, "c": connection, "h": digest},
            )
            conn.execute(
                text(
                    "INSERT INTO workouts (owner_id, connection_id, workout_id, start_at, end_at, "
                    "source_tz, payload_hash) VALUES (:o, :c, 1, '2026-11-02 08:00:00+00', "
                    "'2026-11-02 09:00:00+00', 'Etc/UTC', :h)"
                ),
                {"o": owner_id, "c": connection, "h": digest},
            )
            conn.execute(
                text(
                    "INSERT INTO sync_cursors (owner_id, connection_id, stream) "
                    "VALUES (:o, :c, 'measures')"
                ),
                {"o": owner_id, "c": connection},
            )
            conn.execute(
                text("INSERT INTO data_revisions (owner_id, stream) VALUES (:o, 'measures')"),
                {"o": owner_id},
            )
            conn.execute(
                text(
                    "INSERT INTO sleep_series (owner_id, ts, source_tz, connection_id, series, "
                    "value_raw, unit) VALUES (:o, '2026-11-02 08:00:00+00', 'Etc/UTC', :c, "
                    "'synthetic', 0, 0)"
                ),
                {"o": owner_id, "c": connection},
            )
            conn.execute(
                text(
                    "INSERT INTO intraday (owner_id, ts, source_tz, connection_id, metric, "
                    "value_raw, unit) VALUES (:o, '2026-11-02 08:00:00+00', 'Etc/UTC', :c, "
                    "'synthetic', 0, 0)"
                ),
                {"o": owner_id, "c": connection},
            )
            _plant_derived(conn, owner_id, digest)
    finally:
        engine.dispose()


def _plant_derived(conn: Connection, owner_id: UUID, digest: bytes) -> None:
    """One synthetic row in each derived table (#181)."""
    for sql in (
        "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
        "source_value_raw, source_unit, conversion, source_grpid, source_type, algo_version, "
        "input_revision, input_fingerprint) VALUES (:o, 'weight', '2026-11-02 08:00:00+00', "
        "'2026-11-02 08:00:00+00', 'Etc/UTC', 0, 0, 0, 'synthetic', 1, 1, 'normalize/1', 0, :h)",
        "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, mean, min, max, "
        "mad, n, source_groups, algo_version, input_revision, input_fingerprint) VALUES (:o, "
        "'weight', '2026-11-02', 'median', 0, 0, 0, 0, 0, 1, '{1}', 'daily/1', 0, :h)",
        "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
        "origin, subject, window_start, window_end, input_revision, input_fingerprint) VALUES "
        "(:o, 'Q1', '1', 'system', 'info', 'high', 'live', 'user', '2026-11-02', "
        "'2026-11-02', 0, :h)",
        "INSERT INTO scores (owner_id, kind, day, components, algo_version, input_revision, "
        "input_fingerprint) VALUES (:o, 'his', '2026-11-02', '{\"status\": \"not_set\"}', "
        "'his/1', 0, :h)",
        "INSERT INTO user_profiles (owner_id) VALUES (:o)",
    ):
        conn.execute(text(sql), {"o": owner_id, "h": digest})


def _count(url: str, table: str, owner_id: UUID) -> int:
    if _NAME.fullmatch(table) is None:
        raise AssertionError(table)
    counted = _scalar(
        url,
        f"SELECT count(*) FROM public.{table} WHERE owner_id = :o",  # noqa: S608
        o=owner_id,
    )
    if isinstance(counted, int) and not isinstance(counted, bool):
        return counted
    raise AssertionError("count is not an integer")


async def test_account_delete_revokes_and_removes_every_owned_row(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner)
    other = make_context(await env.user("kept-account@example.com"))
    other_grant = await store_grant(
        env.factory,
        env.app.state.keyring,
        other,
        linked.app_id,
        2002,
        "user.metrics",
        "synthetic-access-token-bbb",
        "synthetic-refresh-token-bbb",
        expires_in=10800,
    )
    _run(
        database_url,
        "DELETE FROM webhook_events WHERE dedupe_key IN ('synthetic-event-a', 'synthetic-event-b')",
    )
    _run(
        database_url,
        "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
        "dedupe_key) "
        "VALUES (:a, :u, 1, 1, 2, :k)",
        a=linked.app_id,
        u=linked.userid,
        k="synthetic-event-a",
    )
    _run(
        database_url,
        "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
        "dedupe_key) "
        "VALUES (:a, :u, 1, 1, 2, :k)",
        a=linked.app_id,
        u=2002,
        k="synthetic-event-b",
    )
    catalog = [(str(name), bool(cascades)) for name, cascades in _rows(database_url, _OWNED)]
    names = {name for name, _ in catalog}
    assert names == {
        "activity_daily",
        "connections",
        "data_revisions",
        "devices",
        "ecg_recordings",
        "findings",
        "intraday",
        "job_events",
        "jobs",
        "measure_groups",
        "measures",
        "metric_daily",
        "observations",
        "scores",
        "sleep_series",
        "sleep_summaries",
        "sync_cursors",
        "user_profiles",
        "workouts",
    }
    for name, cascades in catalog:
        assert cascades or name in EXPLICIT_PURGE, name
    assert set(EXPLICIT_PURGE) <= names
    _partitions(database_url, True)
    try:
        _plant(database_url, linked.ctx.user_id, linked.connection_id, "a")
        _plant(database_url, other.user_id, other_grant.id, "b")
        for name in names:
            assert _count(database_url, name, linked.ctx.user_id) == 1
            assert _count(database_url, name, other.user_id) == 1
        result = await delete_owner(
            env.factory,
            env.app.state.keyring,
            linked.ctx,
            stepped_up=True,
            inner=linked.inner,
        )
        endpoints = [entry.endpoint for entry in linked.fake.log]
        assert endpoints.count("notify:revoke") == 2
        assert "v2/oauth2:revoke" in endpoints
        assert result.token_revoked is True
        for name in names:
            assert _count(database_url, name, linked.ctx.user_id) == 0, name
            assert _count(database_url, name, other.user_id) == 1, name
        assert _hits(database_url, linked.access) == []
        assert _hits(database_url, linked.refresh) == []
        assert (
            _scalar(
                database_url,
                "SELECT count(*) FROM webhook_events WHERE dedupe_key = 'synthetic-event-a'",
            )
            == 0
        )
        assert (
            _scalar(
                database_url,
                "SELECT count(*) FROM webhook_events WHERE dedupe_key = 'synthetic-event-b'",
            )
            == 1
        )
        events = _rows(database_url, "SELECT kind, user_id::text, detail::text FROM auth_events")
        assert len(events) == 1
        kind, user_id, detail = events[0]
        assert kind == "account_deleted"
        assert user_id is None
        assert detail in (None, "null")
        assert linked.access not in (detail or "")
        assert linked.refresh not in (detail or "")
        third = make_context(await env.user("relink@example.com"))
        await store_grant(
            env.factory,
            env.app.state.keyring,
            third,
            linked.app_id,
            linked.userid,
            "user.metrics",
            "synthetic-access-token-ccc",
            "synthetic-refresh-token-ccc",
            expires_in=10800,
        )
    finally:
        _partitions(database_url, False)


async def test_delete_imported_removes_only_that_owners_rows(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner)
    other = make_context(await env.user("kept-import@example.com"))
    other_grant = await store_grant(
        env.factory,
        env.app.state.keyring,
        other,
        linked.app_id,
        2002,
        "user.metrics",
        "synthetic-access-token-ddd",
        "synthetic-refresh-token-ddd",
        expires_in=10800,
    )
    _partitions(database_url, True)
    try:
        _plant(database_url, linked.ctx.user_id, linked.connection_id, "c")
        _plant(database_url, other.user_id, other_grant.id, "d")
        await finish_disconnect(
            env.factory,
            env.app.state.keyring,
            linked.ctx,
            linked.connection_id,
            stepped_up=True,
            delete_imported=True,
            inner=linked.inner,
        )
        for name in ("devices", "measures", "jobs", "job_events", "sleep_series"):
            assert _count(database_url, name, linked.ctx.user_id) == 0, name
            assert _count(database_url, name, other.user_id) == 1, name
        assert (
            _scalar(
                database_url,
                "SELECT count(*) FROM connections WHERE owner_id = :o",
                o=linked.ctx.user_id,
            )
            == 0
        )
        assert (
            _scalar(
                database_url,
                "SELECT count(*) FROM connections WHERE owner_id = :o",
                o=other.user_id,
            )
            == 1
        )
        third = make_context(await env.user("relink-import@example.com"))
        await store_grant(
            env.factory,
            env.app.state.keyring,
            third,
            linked.app_id,
            linked.userid,
            "user.metrics",
            "synthetic-access-token-eee",
            "synthetic-refresh-token-eee",
            expires_in=10800,
        )
    finally:
        _partitions(database_url, False)


async def test_account_delete_requires_step_up(env: Env, database_url: str) -> None:
    ctx = make_context(await env.user("still-here@example.com"))
    with pytest.raises(DisconnectRefused, match="step_up_required"):
        await delete_owner(env.factory, env.app.state.keyring, ctx, stepped_up=False)
    assert _scalar(database_url, "SELECT count(*) FROM users WHERE id = :o", o=ctx.user_id) == 1
