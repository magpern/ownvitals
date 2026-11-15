"""The export's allowlist covers the catalog (ADR 0013 section 1; #211).

Every owned table is exported (``GENERIC`` or ``SPECIAL``) or listed in ``NOT_EXPORTED_TABLES``
with a reason; every column of an exported table is exported or in ``NOT_EXPORTED_COLUMNS`` with a
reason. The same holds for every account table of ADR 0003 section 1 (``ownvitals.auth.exports``).
A new owned or account table, or a new column in one that is exported (``users`` included),
fails here until it is classified. No binary column is ever exported (hashes, ciphertext and the
raw signal frame are bytes; the signal is exported decoded).
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from auth_tables_scan import TABLE_NAMES as ACCOUNT_TABLES
from contexttools import owned_tables
from ownvitals.auth.exports import (
    ACCOUNT_COLUMNS,
    ACCOUNT_NOT_EXPORTED,
    ACCOUNT_TABLES_NOT_EXPORTED,
)
from ownvitals.reports.export_allowlist import (
    GENERIC,
    NOT_EXPORTED_COLUMNS,
    NOT_EXPORTED_TABLES,
    SPECIAL,
    SPECIAL_COLUMNS,
    exported_columns,
    generic_tables,
)

SYNTHETIC = True
_COLUMNS = text(
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_catalog.pg_attribute a "
    "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid JOIN pg_catalog.pg_namespace n "
    "ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = :t AND a.attnum > 0 "
    "AND NOT a.attisdropped"
)


@pytest.fixture(name="catalog", scope="module")
def catalog_fixture(role_urls: object) -> Iterator[str]:
    from dbtools import migrated

    with migrated(role_urls.owner):  # pyright: ignore[reportAttributeAccessIssue]
        yield role_urls.owner  # pyright: ignore[reportAttributeAccessIssue]


def _columns(url: str, table: str) -> dict[str, str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {str(n): str(t) for n, t in conn.execute(_COLUMNS, {"t": table}).all()}
    finally:
        engine.dispose()


def test_the_lists_do_not_overlap() -> None:
    generic = generic_tables()
    assert len(generic) == len(GENERIC)
    assert len({spec.file for spec in GENERIC}) == len(GENERIC)
    assert not generic & set(SPECIAL)
    assert not (generic | set(SPECIAL)) & set(NOT_EXPORTED_TABLES)
    assert set(SPECIAL) == set(SPECIAL_COLUMNS)
    for table, excluded in NOT_EXPORTED_COLUMNS.items():
        assert not set(exported_columns(table)) & set(excluded), table
        assert all(reason.strip() for reason in excluded.values()), table
    for table, excluded in ACCOUNT_NOT_EXPORTED.items():
        assert not set(ACCOUNT_COLUMNS[table]) & set(excluded), table
    assert all(reason.strip() for reason in NOT_EXPORTED_TABLES.values())
    assert all(reason.strip() for reason in ACCOUNT_TABLES_NOT_EXPORTED.values())


def test_every_owned_table_and_column_is_classified(catalog: str) -> None:
    owned = owned_tables(catalog)
    classified = generic_tables() | set(SPECIAL) | set(NOT_EXPORTED_TABLES)
    assert owned == classified, (sorted(owned - classified), sorted(classified - owned))
    for table in generic_tables() | set(SPECIAL):
        columns = set(_columns(catalog, table))
        listed = set(exported_columns(table)) | set(NOT_EXPORTED_COLUMNS[table])
        assert columns == listed, (table, sorted(columns ^ listed))


def test_no_binary_column_is_exported_as_is(catalog: str) -> None:
    for spec in GENERIC:
        types = _columns(catalog, spec.table)
        for column in spec.columns:
            assert types[column] != "bytea", (spec.table, column)
    # The recordings' frame is bytes in the table and exported decoded (export_ecg), never raw.
    types = _columns(catalog, "ecg_recordings")
    assert [c for c in SPECIAL_COLUMNS["ecg_recordings"] if types[c] == "bytea"] == ["signal"]


def test_every_account_table_and_column_is_classified(catalog: str) -> None:
    tables = set(ACCOUNT_TABLES)
    assert tables == set(ACCOUNT_COLUMNS) | set(ACCOUNT_TABLES_NOT_EXPORTED)
    for table, exported in ACCOUNT_COLUMNS.items():
        columns = _columns(catalog, table)
        listed = set(exported) | set(ACCOUNT_NOT_EXPORTED[table])
        assert set(columns) == listed, (table, sorted(set(columns) ^ listed))
        assert all(columns[c] != "bytea" for c in exported), table
