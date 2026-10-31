"""Database access. Nothing here connects or runs at import time."""

from ownvitals.db.engine import (
    CONNECT_TIMEOUT_SECONDS,
    check_ready,
    create_engine,
    create_migrations_engine,
    create_session_factory,
    event_loop_factory,
)
from ownvitals.db.role_check import DatabaseRoleError, verify_database_role
from ownvitals.db.session import (
    TransactionGuard,
    TransactionOwnedError,
    system_session,
    user_session,
)
from ownvitals.db.tables import SYSTEM_TABLES

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "SYSTEM_TABLES",
    "DatabaseRoleError",
    "TransactionGuard",
    "TransactionOwnedError",
    "check_ready",
    "create_engine",
    "create_migrations_engine",
    "create_session_factory",
    "event_loop_factory",
    "system_session",
    "user_session",
    "verify_database_role",
]
