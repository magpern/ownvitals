"""stored flags and the admin audit log (ADR 0002 sections 3 and 4; #12)

Revision ID: 0006_admin
Revises: 0005_auth
Create Date: 2026-11-01

Two system tables (``ownvitals.db.SYSTEM_TABLES``: no ``owner_id``, no row-level security), from
classbeat's ``0007_tiers`` at ``d806051`` with ownvitals' keys:

- ``app_settings``: one row per flag (``signup_mode``, ``experimental_findings``,
  ``ai_insights``, ``banner``; the check refuses any other). The column is ``flag``, not
  classbeat's ``key``: the crypto guard reads a ``key`` column as a secret. The app role may only read it;
  the admin area writes it as ``ownvitals_maint``. A cleared flag is the JSON ``null``.
- ``admin_audit``: one row per admin transaction or refusal. Its id is a ``bigserial``
  (ADR 0002 section 3: the webhook-secret role gets ``USAGE`` on its sequence in M1). It holds
  keys and meanings, never secrets, tokens or health values (``ownvitals.admin.audit``).
  **Append-only by privileges** (Architect decision, #12): the app role holds nothing on it,
  ``ownvitals_maint`` (``docker/postgres/maint/30-maint.sql``) only ``INSERT`` and column
  ``SELECT``; no runtime role may ``UPDATE``, ``DELETE`` or ``TRUNCATE`` it. There is no trigger
  (ADR 0005 section 3 allows one; none is installed), so the owner role, used only by
  ``migrate``, can still change rows (``docs/dev/admin.md``). The two foreign keys to ``users``
  are ``ON DELETE SET NULL``: a referential action runs as the table owner, so deleting an
  account keeps its rows with the email.

The default privileges of ``ownvitals_owner`` give ``ownvitals_app`` full DML on new tables; this
migration narrows them (``docker/postgres/init/10-roles.sql`` restores the same after its blanket
re-grant). This migration moves no data, and no queue function reads these tables.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_admin"
down_revision: str | Sequence[str] | None = "0005_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING_KEYS = "'signup_mode', 'experimental_findings', 'ai_insights', 'banner'"

APP_SETTINGS = f"""
    CREATE TABLE public.app_settings (
        flag text PRIMARY KEY CONSTRAINT app_settings_flag CHECK (flag IN ({SETTING_KEYS})),
        value jsonb NOT NULL
            CONSTRAINT app_settings_value CHECK (octet_length(value::text) <= 2048),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by uuid
    )"""

ADMIN_AUDIT = """
    CREATE TABLE public.admin_audit (
        id bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        actor_id uuid REFERENCES public.users (id) ON DELETE SET NULL,
        actor_email text NOT NULL
            CONSTRAINT admin_audit_actor_email CHECK (char_length(actor_email) BETWEEN 1 AND 320),
        action text NOT NULL
            CONSTRAINT admin_audit_action CHECK (action ~ '^[a-z][a-z0-9_.]{0,59}$'),
        target_user_id uuid REFERENCES public.users (id) ON DELETE SET NULL,
        target_kind text
            CONSTRAINT admin_audit_target_kind CHECK (target_kind ~ '^[a-z][a-z0-9_]{0,39}$'),
        target_id text
            CONSTRAINT admin_audit_target_id CHECK (char_length(target_id) BETWEEN 1 AND 100),
        params jsonb NOT NULL DEFAULT '{}'::jsonb CONSTRAINT admin_audit_params
            CHECK (jsonb_typeof(params) = 'object' AND octet_length(params::text) <= 2048),
        outcome text NOT NULL
            CONSTRAINT admin_audit_outcome CHECK (outcome IN ('ok', 'refused', 'error')),
        reason text CONSTRAINT admin_audit_reason CHECK (reason ~ '^[a-z][a-z0-9_]{0,59}$'),
        request_id text NOT NULL
            CONSTRAINT admin_audit_request_id CHECK (char_length(request_id) BETWEEN 1 AND 100),
        ip_hash bytea NOT NULL CONSTRAINT admin_audit_ip_hash CHECK (octet_length(ip_hash) = 32)
    )"""

INDEXES = (
    "CREATE INDEX admin_audit_created ON public.admin_audit (created_at)",
    "CREATE INDEX admin_audit_actor ON public.admin_audit (actor_id)",
    "CREATE INDEX admin_audit_target_user ON public.admin_audit (target_user_id)",
)

# The app role's privileges on the new relations (guard 2, ``tests/role_guard.py``).
APP_GRANTS = (
    "REVOKE ALL ON TABLE public.app_settings FROM ownvitals_app",
    "GRANT SELECT ON TABLE public.app_settings TO ownvitals_app",
    "REVOKE ALL ON TABLE public.admin_audit FROM ownvitals_app",
    "REVOKE ALL ON SEQUENCE public.admin_audit_id_seq FROM ownvitals_app",
)


def upgrade() -> None:
    op.execute(APP_SETTINGS)
    op.execute(ADMIN_AUDIT)
    for statement in (*INDEXES, *APP_GRANTS):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.admin_audit")
    op.execute("DROP TABLE IF EXISTS public.app_settings")
