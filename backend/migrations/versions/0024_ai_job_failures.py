"""The chat assistant's job failure categories (ADR 0014 sections 5 and 7; #208)

Revision ID: 0024_ai_job_failures
Revises: 0023_ai_audit
Create Date: 2026-11-09

An ``ai_chat_turn`` job that ends without an answer says why with one of three new
``jobs.error_category`` values, so the browser can word it (ADR 0015 section 6) without any text
from the model or the provider:

- ``ai_not_enabled``: the flag went off or the consent was withdrawn before the call or the
  store;
- ``ai_budget_exhausted``: the monthly cap refused the reservation (ADR 0014 section 7: "no
  call; the chat says ... used up");
- ``ai_unavailable``: anything else (an owner lock, the provider's refusal or failure, a turn
  that ended without an answer).

None is retried (``ownvitals.jobs.errors.RETRYABLE`` does not change). Expand only: the check
admits three more values; no row changes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_ai_job_failures"
down_revision: str | Sequence[str] | None = "0023_ai_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ADR 0005 section 6 and this revision; the same list as ``ownvitals.jobs.errors.CATEGORIES``.
CATEGORIES = (
    "'upstream_unavailable', 'rate_limited', 'lease_lost', 'not_connected', 'needs_reconnect', "
    "'key_unavailable', 'decrypt_failed', 'invalid_input', 'account_disabled', "
    "'outbound_blocked', 'internal', 'ai_not_enabled', 'ai_budget_exhausted', 'ai_unavailable'"
)
# The list of 0003_jobs, which a downgrade restores.
PREVIOUS = (
    "'upstream_unavailable', 'rate_limited', 'lease_lost', 'not_connected', 'needs_reconnect', "
    "'key_unavailable', 'decrypt_failed', 'invalid_input', 'account_disabled', "
    "'outbound_blocked', 'internal'"
)
_DROP = "ALTER TABLE public.jobs DROP CONSTRAINT jobs_error_category"


def _add(categories: str) -> str:
    return (
        "ALTER TABLE public.jobs ADD CONSTRAINT jobs_error_category "
        f"CHECK (error_category IN ({categories}))"
    )


def upgrade() -> None:
    op.execute(_DROP)
    op.execute(_add(CATEGORIES))


def downgrade() -> None:
    # Forced RLS would hide the rows from this role, so the update would not see them. A job that
    # failed with a category only this revision admits keeps its failure as ``internal``.
    op.execute("ALTER TABLE public.jobs DISABLE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE public.jobs SET error_category = 'internal' "
        "WHERE error_category IN ('ai_not_enabled', 'ai_budget_exhausted', 'ai_unavailable')"
    )
    op.execute("ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.jobs FORCE ROW LEVEL SECURITY")
    op.execute(_DROP)
    op.execute(_add(PREVIOUS))
