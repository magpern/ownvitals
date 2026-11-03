"""qr login: QR approve-login requests and the approved session's origin (ADR 0011 section 5)

Revision ID: 0015_qr_login
Revises: 0014_passkeys
Create Date: 2026-11-03

One new account system table (ADR 0002 section 3: the desktop has no user until a phone approves,
so the row is not owned; reached only through ``ownvitals.auth``, guard 8), and four columns on
``sessions``:

- ``qr_logins``: one row per QR approve-login request. The nonce is stored only as its SHA-256.
  The row is bound to the desktop's pre-session (``presession_id``; ``SET NULL`` when the
  pre-session goes, which ends the row's use), and once scanned to the approving user
  (``CASCADE``) and the approving session (``SET NULL``: a revoked approving session makes
  ``complete`` fail, ADR 0011 section 5 "Account state"). At most one ``pending`` or ``scanned``
  row per pre-session (a partial unique index). The deadlines are 60 s to scan, 120 s to approve
  and 30 s to complete; the hourly purge deletes a row one hour after its last deadline.
- ``sessions.origin``: ``signin`` (every session so far) or ``qr_approval``; carried over every
  rotation, so a QR-born session can never approve another QR login.
- ``sessions.mfa_by_approval``: the session is full because a code was entered on the approving
  phone for this approval (ADR 0011 section 5, amending ADR 0003 sections 2 and 7). It never
  counts for the admin role; a TOTP verified in the session itself clears it.
- ``sessions.approved_factor`` and ``sessions.approved_by_session_id``: the factor of the
  approval and the approving session's id (a record only: no foreign key, the approving session
  rotates and ends on its own).

The default privileges of ``ownvitals_owner`` give ``ownvitals_app`` SELECT, INSERT, UPDATE and
DELETE on the new table and nothing else, so no GRANT is needed (``tests/test_auth_schema.py``).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_qr_login"
down_revision: str | Sequence[str] | None = "0014_passkeys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QR_LOGINS = """
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nonce_hash bytea NOT NULL UNIQUE
        CONSTRAINT qr_logins_nonce_hash CHECK (octet_length(nonce_hash) = 32),
    presession_id uuid REFERENCES pre_sessions (id) ON DELETE SET NULL,
    state text NOT NULL DEFAULT 'pending' CONSTRAINT qr_logins_state CHECK (state IN
        ('pending', 'scanned', 'approved', 'completed', 'denied', 'expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    scan_deadline timestamptz NOT NULL,
    desktop_ip text CONSTRAINT qr_logins_desktop_ip CHECK (char_length(desktop_ip) <= 64),
    desktop_browser text NOT NULL
        CONSTRAINT qr_logins_desktop_browser CHECK (char_length(desktop_browser) <= 32),
    desktop_os text NOT NULL CONSTRAINT qr_logins_desktop_os CHECK (char_length(desktop_os) <= 32),
    desktop_place text
        CONSTRAINT qr_logins_desktop_place CHECK (char_length(desktop_place) BETWEEN 1 AND 64),
    approver_user_id uuid REFERENCES users (id) ON DELETE CASCADE,
    approver_session_id uuid REFERENCES sessions (id) ON DELETE SET NULL,
    scanned_at timestamptz,
    approve_deadline timestamptz,
    approved_at timestamptz,
    approved_factor text
        CONSTRAINT qr_logins_approved_factor CHECK (approved_factor IN ('totp', 'recovery_code')),
    complete_deadline timestamptz,
    CONSTRAINT qr_logins_pending CHECK (state <> 'pending' OR (approver_user_id IS NULL
        AND approver_session_id IS NULL AND scanned_at IS NULL AND approve_deadline IS NULL)),
    CONSTRAINT qr_logins_scanned CHECK (state NOT IN ('scanned', 'approved', 'completed')
        OR (approver_user_id IS NOT NULL AND scanned_at IS NOT NULL
            AND approve_deadline IS NOT NULL)),
    CONSTRAINT qr_logins_approved CHECK (state NOT IN ('approved', 'completed')
        OR (approved_at IS NOT NULL AND approved_factor IS NOT NULL
            AND complete_deadline IS NOT NULL))"""

INDEXES = [
    # At most one pending or scanned row per pre-session (ADR 0011 section 5, step 1).
    "CREATE UNIQUE INDEX qr_logins_one_live_per_presession ON qr_logins (presession_id) "
    "WHERE state IN ('pending', 'scanned')",
    "CREATE INDEX qr_logins_approver_user_id_idx ON qr_logins (approver_user_id)",
    "CREATE INDEX qr_logins_approver_session_id_idx ON qr_logins (approver_session_id)",
    "CREATE INDEX qr_logins_scan_deadline_idx ON qr_logins (scan_deadline)",
]

SESSION_COLUMNS = (
    "ALTER TABLE public.sessions "
    "ADD COLUMN origin text NOT NULL DEFAULT 'signin' "
    "CONSTRAINT sessions_origin CHECK (origin IN ('signin', 'qr_approval')), "
    "ADD COLUMN mfa_by_approval boolean NOT NULL DEFAULT false, "
    "ADD COLUMN approved_factor text "
    "CONSTRAINT sessions_approved_factor CHECK (approved_factor IN ('totp', 'recovery_code')), "
    "ADD COLUMN approved_by_session_id uuid, "
    "ADD CONSTRAINT sessions_qr_approval CHECK ((origin = 'qr_approval') = "
    "(approved_factor IS NOT NULL AND approved_by_session_id IS NOT NULL)), "
    "ADD CONSTRAINT sessions_mfa_by_approval CHECK (NOT mfa_by_approval OR origin = 'qr_approval')"
)


def upgrade() -> None:
    op.execute(SESSION_COLUMNS)
    op.execute(f"CREATE TABLE public.qr_logins ({QR_LOGINS})")
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.qr_logins CASCADE")
    op.execute(
        "ALTER TABLE public.sessions DROP CONSTRAINT IF EXISTS sessions_mfa_by_approval, "
        "DROP CONSTRAINT IF EXISTS sessions_qr_approval, "
        "DROP COLUMN IF EXISTS approved_by_session_id, DROP COLUMN IF EXISTS approved_factor, "
        "DROP COLUMN IF EXISTS mfa_by_approval, DROP COLUMN IF EXISTS origin"
    )
