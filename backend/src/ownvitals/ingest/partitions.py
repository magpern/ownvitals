"""Month partitions for ``sleep_series``, ``intraday`` and ``observations`` (#181).

Callers open one short transaction per month and commit it before the insert. The function
takes an advisory lock on the parent, so it must not run inside the insert transaction.
Months outside [2009-01-01, now + 3 months) are not requested: the function refuses them,
and those rows are skipped instead.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PARTITIONED = frozenset({"sleep_series", "intraday", "observations"})
_ENSURE = text("SELECT ensure_month_partitions(:table, :month, :month)")


async def ensure_month(db: AsyncSession, table: str, month: date) -> None:
    """Create ``month`` (the first of the month) when it is missing. One call, one month."""
    if table not in PARTITIONED:
        raise ValueError("not a partitioned table")
    if month.day != 1:
        raise ValueError("a partition month starts on the first")
    await db.execute(_ENSURE, {"table": table, "month": month})
