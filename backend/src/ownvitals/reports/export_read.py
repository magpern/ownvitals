"""Pages of the user's owned rows for the export (ADR 0013 sections 1 and 4; #211).

Every read runs in the export job's fenced session: the owner's ``user_session`` under forced RLS
(so the builder cannot reach another user's rows even by a bug), and every statement also filters
``owner_id`` itself (ADR 0002). Only the columns of ``export_allowlist.GENERIC`` are selected; the
statement is built from that allowlist, never from input, and each identifier is checked against
a plain-name pattern first. Pages are keyset pages on ``id`` (every owned table has a UUID ``id``),
so a page is one short transaction and the job checkpoints between them.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.reports.export_allowlist import TableSpec

PAGE_ROWS: Final = 2000
_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,62}")


def _checked(name: str) -> str:
    if _NAME.fullmatch(name) is None:
        raise ValueError("not a plain identifier")
    return name


def page_sql(spec: TableSpec) -> str:
    if spec.columns[:1] != ("id",):
        raise ValueError("an exported table's first column is its id (the page key)")
    columns = ", ".join(_checked(c) for c in spec.columns)
    table = _checked(spec.table)
    return (
        f"SELECT {columns} FROM public.{table} "  # noqa: S608 - allowlisted identifiers
        "WHERE owner_id = :owner AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
        "ORDER BY id LIMIT :limit"
    )


async def read_page(
    db: AsyncSession,
    owner: UUID,
    spec: TableSpec,
    after: UUID | None,
    *,
    limit: int = PAGE_ROWS,
) -> Sequence[Mapping[str, Any]]:
    """The next page of ``spec``'s rows of ``owner`` after ``after`` (``None``: the first)."""
    params = {"owner": owner, "after": after, "limit": limit}
    rows = (await db.execute(text(page_sql(spec)), params)).mappings().all()
    return [dict(row) for row in rows]
