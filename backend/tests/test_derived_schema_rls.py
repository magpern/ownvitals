"""Row-level security on the derived tables (#181), in the request and worker contexts of the
harness (``ctx.py``), as the app role. Two synthetic users; nothing crosses.

Each table gets a test-only repository (``TableRows``) shaped like ``ctx.Rows``: one text column
plays the row's name. The names are valid in every table: ``body_path:`` makes them a score
kind, and every name column accepts the colon. ``filtered=False`` drops the repository's own
owner filter, so RLS is the only wall left and must still hold alone. Synthetic rows only; no
real account or measurement.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ctx import EXECUTION_CONTEXTS, Rows
from jobtools import engine_for, ensure_user
from ownvitals.context import UserContext
from ownvitals.derived.tables import DERIVED_OWNED

pytestmark = pytest.mark.usefixtures("schema")
HASH = b"\x44" * 32  # a labelled synthetic fingerprint

# table -> (the column that holds the name, an INSERT of one row named :name for :owner)
TABLES: dict[str, tuple[str, str]] = {
    "observations": (
        "metric_key",
        "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
        "source_value_raw, source_unit, conversion, algo_version, input_revision, "
        "input_fingerprint) VALUES (:owner, :name, '2026-10-28 06:30:00+00', "
        "'2026-10-28 06:30:00+00', 'Etc/UTC', 1, 1, 0, 'synthetic', 'synthetic/1', 0, :h)",
    ),
    "metric_daily": (
        "metric_key",
        "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, mean, min, max, "
        "mad, n, source_groups, algo_version, input_revision, input_fingerprint) VALUES "
        "(:owner, :name, '2026-10-28', 'median', 1, 1, 1, 1, 0, 1, '{1}', 'synthetic/1', 0, :h)",
    ),
    "findings": (
        "subject",
        "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
        "origin, subject, window_start, window_end, input_revision, input_fingerprint) VALUES "
        "(:owner, 'C1', '1', 'trend', 'attention', 'low', 'live', :name, '2026-10-01', "
        "'2026-10-28', 0, :h)",
    ),
    "scores": (
        "kind",
        "INSERT INTO scores (owner_id, kind, day, value, components, algo_version, "
        "input_revision, input_fingerprint) VALUES (:owner, :name, '2026-10-28', 1, "
        "'{\"status\": \"computed\"}', 'synthetic/1', 0, :h)",
    ),
    "user_profiles": (
        "day_tz",
        "INSERT INTO user_profiles (owner_id, day_assignment, day_tz) "
        "VALUES (:owner, 'fixed_tz', :name)",
    ),
}


@dataclass(frozen=True)
class TableRows(Rows):
    """The test-only repository of one derived table. Like a real repository it filters on
    ``owner_id`` itself (unless ``filtered`` is off); inserts carry the caller's owner."""

    table: str = "findings"

    def _sql(self, verb: str, where: str) -> Any:
        """``verb`` and ``where`` are fixed fragments; the table and column come from ``TABLES``."""
        own = " AND owner_id = :owner" if self.filtered else ""
        sql = f"{verb} public.{self.table} WHERE {where}{own}"
        return text(sql.replace("{c}", TABLES[self.table][0]))

    async def add(self, db: AsyncSession, ctx: UserContext, name: str) -> UUID:
        params = {"owner": ctx.user_id, "name": name, "h": HASH}
        await db.execute(text(TABLES[self.table][1]), params)
        found = await db.execute(self._sql("SELECT id FROM", "{c} = :name"), params)
        return found.scalar_one()

    async def names(self, db: AsyncSession, ctx: UserContext) -> list[str]:
        result = await db.execute(self._sql("SELECT {c} FROM", "true"), {"owner": ctx.user_id})
        return sorted(result.scalars())

    async def find(self, db: AsyncSession, ctx: UserContext, row_id: UUID) -> str | None:
        result = await db.execute(
            self._sql("SELECT {c} FROM", "id = :id"), {"id": row_id, "owner": ctx.user_id}
        )
        return result.scalar_one_or_none()

    async def delete(self, db: AsyncSession, ctx: UserContext, row_id: UUID) -> bool:
        result = await db.execute(
            self._sql("DELETE FROM", "id = :id"), {"id": row_id, "owner": ctx.user_id}
        )
        return result.rowcount == 1  # pyright: ignore[reportAttributeAccessIssue]


@pytest.fixture
def partition(schema: None, app_database_url: str, database_url: str) -> Iterator[None]:
    """October 2026 of ``observations``, made by the definer as a writer would make it."""
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "SELECT ensure_month_partitions('observations', DATE '2026-10-01', "
                "DATE '2026-10-01')"
            )
        )
    yield
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS public.observations_y2026m10"))


def _users(database_url: str) -> tuple[UUID, UUID]:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        ensure_user(conn, alice)
        ensure_user(conn, bob)
    return alice, bob


def test_every_derived_table_is_covered() -> None:
    assert set(TABLES) == set(DERIVED_OWNED)


@pytest.mark.usefixtures("partition")
@pytest.mark.parametrize("filtered", [True, False], ids=["filtered", "rls-only"])
@pytest.mark.parametrize("table", sorted(TABLES))
@pytest.mark.parametrize("context", sorted(EXECUTION_CONTEXTS))
async def test_no_row_crosses_between_two_users(
    context: str, table: str, filtered: bool, app_database_url: str, database_url: str
) -> None:
    alice, bob = _users(database_url)
    rows = TableRows(filtered=filtered, table=table)
    async with EXECUTION_CONTEXTS[context](app_database_url, rows=rows) as ctx:
        alice_id = await ctx.add(alice, "body_path:alice_row")
        bob_id = await ctx.add(bob, "body_path:bob_row")
        assert await ctx.names(alice) == ["body_path:alice_row"]
        assert await ctx.names(bob) == ["body_path:bob_row"]
        assert await ctx.find(bob, alice_id) is None
        assert await ctx.find(alice, bob_id) is None
        assert await ctx.delete(bob, alice_id) is False
        assert await ctx.find(alice, alice_id) == "body_path:alice_row"
        assert await ctx.delete(alice, alice_id) is True
        assert await ctx.names(alice) == []
        assert await ctx.names(bob) == ["body_path:bob_row"]
    with engine_for(database_url) as engine, engine.connect() as conn:
        left = conn.execute(
            text(f"SELECT owner_id FROM public.{table} WHERE owner_id IN (:a, :b)"),  # noqa: S608
            {"a": alice, "b": bob},
        ).scalars()
        assert list(left) == [bob]


@pytest.mark.usefixtures("partition")
@pytest.mark.parametrize("table", sorted(TABLES))
def test_without_a_user_nothing_is_seen_and_no_foreign_owner_is_written(
    table: str, app_database_url: str, database_url: str
) -> None:
    alice, bob = _users(database_url)
    insert = text(TABLES[table][1])
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(insert, {"owner": alice, "name": "body_path:alice_row", "h": HASH})
    with engine_for(app_database_url) as engine:
        with engine.connect() as conn:
            seen = conn.execute(text(f"SELECT count(*) FROM public.{table}"))  # noqa: S608
            assert seen.scalar_one() == 0
        as_alice = "SELECT set_config('app.user_id', :u, true)"
        update = text(f"UPDATE public.{table} SET owner_id = :b")  # noqa: S608
        with engine.connect() as conn:
            conn.execute(text(as_alice), {"u": str(alice)})
            with pytest.raises(DBAPIError, match="row-level security"), conn.begin_nested():
                conn.execute(insert, {"owner": bob, "name": "body_path:bob_row", "h": HASH})
            with pytest.raises(DBAPIError, match="row-level security"), conn.begin_nested():
                conn.execute(update, {"b": bob})  # handing a row to another owner
            conn.rollback()
        with engine.begin() as conn:
            conn.execute(text(as_alice), {"u": str(bob)})
            changed = conn.execute(update, {"b": bob})
            assert changed.rowcount == 0  # pyright: ignore[reportAttributeAccessIssue]
