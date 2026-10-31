"""Tables that are not per-user (ADR 0002 section 3)."""

from types import MappingProxyType

# table name -> one-line reason it has no ``owner_id`` and no row-level security.
# Every other table in ``public`` is owned; guard 1 (``tests/test_rls_guard.py``) holds both sides
# to the contract. Each issue that adds a system table lists it here with its reason, in the same
# PR: the account tables (#10, reached only through ``ownvitals.auth``), the KEK check table
# (#8), stored flags and the admin audit log (#12); in M1 ``withings_apps``, ``webhook_events``
# and the per-app rate bucket.
SYSTEM_TABLES: MappingProxyType[str, str] = MappingProxyType(
    {
        "alembic_version": "Alembic migration bookkeeping; no app access",
        "connection_keks": "check values of the key ring's ids (ADR 0004 sections 3 and 4), "
        "recorded and read at startup before any user is known; reached only through "
        "ownvitals.crypto.keks",
    }
)
# System tables the app role must have no privilege on at all (guard 2). The others get the
# default DML of ADR 0002 section 5, or the narrower set their migration grants.
NO_APP_ACCESS = frozenset({"alembic_version"})
