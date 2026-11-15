"""``Reader.home_zone``'s newest-zone query against the database: it only looks at confirmed
readings with ``ts <= now`` and ``ts > now - ZONE_LOOKBACK`` (issue #444, review note of #455).

Synthetic users and readings only; the rows are written as setup, the read runs as
``ownvitals_app`` under forced RLS.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ctx import make_context
from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, ensure_user, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.context import UserContext
from ownvitals.views.reads import ZONE_LOOKBACK, reading

pytestmark = pytest.mark.usefixtures("jenv")

SYNTHETIC = True
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
INSERT = (
    "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
    "source_value_raw, source_unit, conversion, quality_flags, source_grpid, source_type, "
    "source_position, algo_version, input_revision, input_fingerprint) VALUES "
    "(:o, 'weight', :ts, :ts, :tz, 1, 1, 0, 'e0:kg', CAST(:flags AS text[]), NULL, NULL, NULL, "
    "'synthetic/1', 1, decode(repeat('ef', 32), 'hex'))"
)


def plant(jenv: JobsEnv, owner: uuid.UUID, ts: datetime, tz: str, flags: str = "{}") -> None:
    jenv.sql(INSERT, o=owner, ts=ts, tz=tz, flags=flags)


def partitions(jenv: JobsEnv) -> None:
    for month in (8, 9, 10, 11):
        end = f"2026-{month + 1:02d}-01" if month < 12 else "2027-01-01"
        jenv.sql(
            f"CREATE TABLE IF NOT EXISTS observations_synthetic_{month} PARTITION OF "
            f"observations FOR VALUES FROM ('2026-{month:02d}-01') TO ('{end}')"
        )


def account(jenv: JobsEnv) -> tuple[UserContext, uuid.UUID]:
    partitions(jenv)
    owner = uuid.uuid4()
    ensure_user(jenv.admin, owner)
    return make_context(owner), owner


async def zone(jenv: JobsEnv, ctx: UserContext) -> str | None:
    async with reading(jenv.factory, ctx) as r:
        return await r.home_zone(NOW)


async def test_a_reading_after_now_does_not_set_the_zone(jenv: JobsEnv) -> None:
    ctx, owner = account(jenv)
    plant(jenv, owner, NOW - timedelta(days=2), "Europe/Oslo")
    plant(jenv, owner, NOW + timedelta(hours=1), "Asia/Tokyo")  # not yet happened
    assert await zone(jenv, ctx) == "Europe/Oslo"


async def test_a_reading_at_now_counts(jenv: JobsEnv) -> None:
    ctx, owner = account(jenv)
    plant(jenv, owner, NOW, "Asia/Tokyo")
    plant(jenv, owner, NOW - timedelta(days=2), "Europe/Oslo")
    assert await zone(jenv, ctx) == "Asia/Tokyo"


async def test_a_reading_older_than_the_lookback_does_not_set_the_zone(jenv: JobsEnv) -> None:
    ctx, owner = account(jenv)
    plant(jenv, owner, NOW - ZONE_LOOKBACK - timedelta(hours=1), "Asia/Tokyo")
    assert await zone(jenv, ctx) is None


async def test_the_lookback_edge_is_exclusive_and_just_inside_counts(jenv: JobsEnv) -> None:
    ctx, owner = account(jenv)
    plant(jenv, owner, NOW - ZONE_LOOKBACK, "Asia/Tokyo")  # ts > floor: the edge is out
    assert await zone(jenv, ctx) is None
    plant(jenv, owner, NOW - ZONE_LOOKBACK + timedelta(hours=1), "Europe/Oslo")
    assert await zone(jenv, ctx) == "Europe/Oslo"


async def test_unconfirmed_readings_do_not_set_the_zone(jenv: JobsEnv) -> None:
    ctx, owner = account(jenv)
    plant(jenv, owner, NOW - timedelta(days=2), "Europe/Oslo")
    plant(jenv, owner, NOW - timedelta(days=1), "Asia/Tokyo", "{unconfirmed}")
    assert await zone(jenv, ctx) == "Europe/Oslo"
