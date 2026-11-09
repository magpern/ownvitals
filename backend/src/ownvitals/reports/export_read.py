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
_HIDE: Final = "AND kind <> ALL(CAST(:hidden AS text[])) "
_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,62}")


def _checked(name: str) -> str:
    if _NAME.fullmatch(name) is None:
        raise ValueError("not a plain identifier")
    return name


def page_sql(spec: TableSpec, *, experimental: bool) -> str:
    """The page statement; without the experimental tier it leaves out the spec's experimental
    ``kind`` values (bound as ``:hidden``)."""
    if spec.columns[:1] != ("id",):
        raise ValueError("an exported table's first column is its id (the page key)")
    columns = ", ".join(_checked(c) for c in spec.columns)
    table = _checked(spec.table)
    return (
        f"SELECT {columns} FROM public.{table} "  # noqa: S608 - allowlisted identifiers
        "WHERE owner_id = :owner AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
        f"{'' if experimental or not spec.experimental_kinds else _HIDE}"
        "ORDER BY id LIMIT :limit"
    )


async def read_page(
    db: AsyncSession,
    owner: UUID,
    spec: TableSpec,
    after: UUID | None,
    *,
    experimental: bool,
    limit: int = PAGE_ROWS,
) -> Sequence[Mapping[str, Any]]:
    """The next page of ``spec``'s rows of ``owner`` after ``after`` (``None``: the first);
    without ``experimental`` (the owner-only switch) no row of an experimental kind."""
    params: dict[str, Any] = {"owner": owner, "after": after, "limit": limit}
    if not experimental and spec.experimental_kinds:
        params["hidden"] = list(spec.experimental_kinds)
    sql = page_sql(spec, experimental=experimental)
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]
