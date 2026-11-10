"""The weekly AI digest: its opt-in, the stored digests and the "digest waiting" mail (ADR 0014
sections 3, 5 and 7; PLAN section 7.4; issue 209)

Revision ID: 0026_ai_digest
Revises: 0025_ai_job_failures
Create Date: 2026-11-10

- ``ai_digest_prefs`` (owned): one row per owner who opted in to the weekly digest, a separate
  opt-in inside the AI settings, default off (no row), covered by the AI consent (ADR 0014
  section 3). Withdrawal of the consent deletes it, so consenting again starts with the digest
  off.
- ``ai_digests`` (owned): one row per owner and digest week (``week_start``, a Monday). ``ready``
  holds the model's text (plain text, bounded like a chat answer); ``skipped_budget`` (the
  monthly cap refused the call, ADR 0014 section 7: "the digest for that week is skipped with
  the same in-app note") and ``unavailable`` (the call failed or ended without an answer) hold no
  text and stop a second try that week. Withdrawal and "delete imported data" delete the rows;
  account deletion removes both tables through the ``owner_id`` cascade.
- ``mail_outbox`` admits the kind ``ai_digest_ready`` and the route ``assistant`` (ADR 0014
  Consequences, ADR 0007): a fixed text that says something is waiting, never the digest.

Expand only: two new owned tables (forced RLS, the ``owner_isolation`` policy) and two wider
checks. The vocabularies are written out here on purpose; ``ownvitals.ai.tables`` and
``ownvitals.notify.outbox`` hold the same lists and the tests compare them with the checks. The
downgrade deletes the outbox rows of the new kind or route first (mail not yet sent; forced
row-level security is lifted for that one statement), then restores the old checks.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0026_ai_digest"
down_revision: str | Sequence[str] | None = "0025_ai_job_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREFS = """
CREATE TABLE public.ai_digest_prefs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    opted_in_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_digest_prefs_owner UNIQUE (owner_id),
    CONSTRAINT ai_digest_prefs_owner_id_id_key UNIQUE (owner_id, id)
)"""

_DIGESTS = """
CREATE TABLE public.ai_digests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    week_start date NOT NULL
        CONSTRAINT ai_digests_monday CHECK (extract(isodow FROM week_start) = 1),
    status text NOT NULL
        CONSTRAINT ai_digests_status CHECK (status IN ('ready', 'skipped_budget', 'unavailable')),
    content text
        CONSTRAINT ai_digests_content CHECK (char_length(content) BETWEEN 1 AND 16000),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_digests_content_iff_ready CHECK ((status = 'ready') = (content IS NOT NULL)),
    CONSTRAINT ai_digests_owner_week UNIQUE (owner_id, week_start),
    CONSTRAINT ai_digests_owner_id_id_key UNIQUE (owner_id, id)
)"""

_KINDS = "'findings_waiting', 'connection_attention', 'export_ready'"
_ROUTES = "'overview', 'findings', 'connections', 'settings_data'"


def _checks(kinds: str, routes: str) -> None:
    op.execute(
        "ALTER TABLE public.mail_outbox "
        "DROP CONSTRAINT mail_outbox_kind, "
        f"ADD CONSTRAINT mail_outbox_kind CHECK (kind IN ({kinds})), "
        "DROP CONSTRAINT mail_outbox_route, "
        f"ADD CONSTRAINT mail_outbox_route CHECK (route IN ({routes}))"
    )


def upgrade() -> None:
    op.execute(_PREFS)
    enable_owner_rls("ai_digest_prefs")
    op.execute(_DIGESTS)
    enable_owner_rls("ai_digests")
    _checks(f"{_KINDS}, 'ai_digest_ready'", f"{_ROUTES}, 'assistant'")


def downgrade() -> None:
    # The table is under forced row-level security, which would hide every row from this
    # session (no user): lifted for the one statement, as the owner, and forced again.
    op.execute("ALTER TABLE public.mail_outbox NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "DELETE FROM public.mail_outbox WHERE kind = 'ai_digest_ready' OR route = 'assistant'"
    )
    op.execute("ALTER TABLE public.mail_outbox FORCE ROW LEVEL SECURITY")
    _checks(_KINDS, _ROUTES)
    op.execute("DROP TABLE IF EXISTS public.ai_digests")
    op.execute("DROP TABLE IF EXISTS public.ai_digest_prefs")
