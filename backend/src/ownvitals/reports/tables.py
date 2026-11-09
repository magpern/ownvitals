"""The export tables of migration ``0022_exports`` (ADR 0013 section 4; #211).

Both are owned: ``owner_id`` cascading from ``users``, forced RLS and the ``owner_isolation``
policy. ``export_chunks`` belongs to an export of the same owner through a composite foreign key
that cascades. The vocabularies are the migration's check constraints, copied there, not
imported; ``tests/test_reports_export_db.py`` holds both equal.
"""

from typing import Final, Literal, get_args

from sqlalchemy import BigInteger, Column, DateTime, Integer, LargeBinary, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import UUID

ExportKind = Literal["data", "doctor_pdf"]
EXPORT_KINDS: Final[tuple[str, ...]] = get_args(ExportKind)
ExportState = Literal["building", "ready", "failed", "expired"]
EXPORT_STATES: Final[tuple[str, ...]] = get_args(ExportState)
Failure = Literal["export_too_large", "build_failed"]
FAILURES: Final[tuple[str, ...]] = get_args(Failure)
CHUNK_MAX_BYTES: Final = 4 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS: Final = 3
# The owned tables of migration 0022, children before parents.
EXPORT_OWNED: Final = ("export_chunks", "exports")

metadata = MetaData()

exports = Table(
    "exports",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("job_id", UUID(as_uuid=True)),
    Column("failure", Text),
    Column("largest", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("ready_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("size", BigInteger),
    Column("sha256", LargeBinary),
    Column("download_attempts", Integer, nullable=False),
    Column("download_count", Integer, nullable=False),
)

export_chunks = Table(
    "export_chunks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("export_id", UUID(as_uuid=True), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("bytes", LargeBinary, nullable=False),
)
