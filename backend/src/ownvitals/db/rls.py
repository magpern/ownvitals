"""Row-level security for owned tables (ADR 0002 section 2). Used by migrations."""

import re

from alembic import op

OWNER_POLICY_NAME = "owner_isolation"
# ``NULLIF`` turns "never set" (NULL) and "set, then the transaction ended" ('') into NULL, so
# without a user the policy matches no row. It never errors and never matches everything.
OWNER_EXPRESSION = "owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid"

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def owner_rls_statements(table: str) -> list[str]:
    """The SQL that gives ``table`` (in schema ``public``) the owner policy."""
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("table must be a lower-case SQL identifier of at most 63 characters")
    return [
        f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY {OWNER_POLICY_NAME} ON public."{table}" '
        f"AS PERMISSIVE FOR ALL TO PUBLIC "
        f"USING ({OWNER_EXPRESSION}) WITH CHECK ({OWNER_EXPRESSION})",
    ]


def enable_owner_rls(table: str) -> None:
    """Enable and force RLS on ``table`` and add the ``owner_isolation`` policy.

    Call it from a migration, right after creating a table with an ``owner_id`` column. Every
    owned table needs it; ``tests/test_rls_guard.py`` fails the PR when one does not have it.
    """
    for statement in owner_rls_statements(table):
        op.execute(statement)
