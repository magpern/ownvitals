"""findings: the rule ids of the v1 rules (#187)

Revision ID: 0016_findings_rule_ids
Revises: 0015_qr_login
Create Date: 2026-11-03

Migration 0011 accepted only a letter and one or two digits as ``findings.rule_id`` and tied the
system tier to exactly ``Q1``. The merged rules (``analysis.rules.core.SPECS``, ADR 0010 §7 and
the decisions of #183/#184 in ``docs/dev/analysis-rules.md``) give C1's experimental part its own
id ``C1x`` and N2 and Q1 one id per part (``N2:guided``, ``Q1:no_weigh_in`` ...), so their
episodes are separate (ADR 0010 §4). The read function (``views.findings_read``) already drops a
stored ``C1x`` row by its id. Both checks now accept exactly those shapes (Architect-approved
schema fix, #187):

- ``findings_rule_id``: a capital, one or two digits, then optionally ``x`` or ``:`` and a part
  of lower-case letters and ``_`` (at most 24);
- ``findings_system_tier``: the system tier exactly when the id is ``Q1`` or ``Q1:<part>``.

Both columns are ``NOT NULL``, so neither check can evaluate to NULL. The downgrade restores the
0011 checks; it first deletes the rows those checks cannot hold (the new ids). They are derived
rows the evaluation writes again from the imported data (#187), and a downgrade below this
revision has no code that reads them. The table forces RLS on its owner too, so the force is
lifted around the delete, inside the migration's transaction, and set again right after it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_findings_rule_ids"
down_revision: str | Sequence[str] | None = "0015_qr_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RULE_ID = "^[A-Z][0-9]{1,2}(x|:[a-z][a-z_]{0,23})?$"
SYSTEM_RULE = "^Q1(:|$)"
_OLD_RULE_ID = "^[A-Z][0-9]{1,2}$"


def _replace(rule_id: str, system: str) -> None:
    op.execute("ALTER TABLE public.findings DROP CONSTRAINT findings_rule_id")
    op.execute("ALTER TABLE public.findings DROP CONSTRAINT findings_system_tier")
    op.execute(
        "ALTER TABLE public.findings ADD CONSTRAINT findings_rule_id "
        f"CHECK (rule_id ~ '{rule_id}')"
    )
    op.execute(
        "ALTER TABLE public.findings ADD CONSTRAINT findings_system_tier "
        f"CHECK ((tier = 'system') = ({system}))"
    )


def upgrade() -> None:
    _replace(RULE_ID, f"rule_id ~ '{SYSTEM_RULE}'")


def downgrade() -> None:
    op.execute("ALTER TABLE public.findings NO FORCE ROW LEVEL SECURITY")
    op.execute(f"DELETE FROM public.findings WHERE rule_id !~ '{_OLD_RULE_ID}'")
    op.execute("ALTER TABLE public.findings FORCE ROW LEVEL SECURITY")
    _replace(_OLD_RULE_ID, "rule_id = 'Q1'")
