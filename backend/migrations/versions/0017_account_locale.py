"""accounts: the language preference (ADR 0015 sections 1 and 2; #249)

Revision ID: 0017_account_locale
Revises: 0016_findings_rule_ids
Create Date: 2026-11-08

``users.locale`` is the account's language: ``NULL`` means "follow this browser", for existing
accounts and for those the CLI or an admin creates. A new account stores the language in effect
at sign-up, carried by the pending sign-up row (email, and Google's hand-off) or by the OAuth
flow row (Google's callback is a provider redirect with no body), so both get the same column.
Each is an allowlisted enum (``CHECK (locale IN (...))``), never free text; the migration that
adds a language widens the three checks. The outbox is not changed (ADR 0015 section 7).

The three are system tables (ADR 0002 section 3): no ``owner_id`` and no RLS, so no policy
changes; the app role's DML grants already cover a new column.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_account_locale"
down_revision: str | Sequence[str] | None = "0016_findings_rule_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCALES = ("en", "sv")  # frontend/src/i18n/locales.json (tests/test_account_locale.py)
TABLES = {
    "users": "users_locale",
    "pending_signups": "pending_signups_locale",
    "oauth_flows": "oauth_flows_locale",
}


def upgrade() -> None:
    allowed = ", ".join(f"'{code}'" for code in LOCALES)
    for table, check in TABLES.items():
        op.execute(
            f"ALTER TABLE public.{table} ADD COLUMN locale text "
            f"CONSTRAINT {check} CHECK (locale IN ({allowed}))"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} DROP COLUMN IF EXISTS locale")
