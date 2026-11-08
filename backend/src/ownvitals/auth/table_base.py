"""The ``MetaData`` and the column helpers of the account tables (``tables``, ``tables_passkeys``).

Moved out of ``tables`` unchanged (#249), so the passkey and QR tables can live in their own
module; only ``ownvitals.auth`` imports it (guard 8).
"""

from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, MetaData, text
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData()

_ID = "gen_random_uuid()"
_NOW = "now()"


def id_column() -> Column[Any]:
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text(_ID))


def user_fk(*, nullable: bool = False, set_null: bool = False) -> Column[Any]:
    return Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL" if set_null else "CASCADE"),
        nullable=nullable or set_null,
    )


def ts(name: str, *, nullable: bool = True, default_now: bool = False) -> Column[Any]:
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=text(_NOW) if default_now else None,
    )
