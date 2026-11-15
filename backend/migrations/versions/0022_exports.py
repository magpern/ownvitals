"""The export row and its chunks (ADR 0013 section 4; #211)

Revision ID: 0022_exports
Revises: 0021_ai_consent
Create Date: 2026-11-09

- ``exports`` (owned): one row per requested archive. ``kind`` is ``data`` (the JSON/CSV export)
  or ``doctor_pdf`` (a later issue); ``state`` moves ``building`` -> ``ready`` -> ``expired``, or
  ``building`` -> ``failed``. A ready row carries its size, its SHA-256, when it became ready and
  when it expires; ``download_attempts`` counts started downloads (at most 3) and
  ``download_count`` the one completed transfer. At most one row per owner and kind is
  ``building`` (a partial unique index, the backstop of the request's own check). An ``expired``
  or ``failed`` row keeps no content and is deleted after 90 days.
- ``export_chunks`` (owned): the archive's bytes in order, at most 4 MiB a row, belonging to an
  export of the same owner (a composite foreign key with ``ON DELETE CASCADE``). The chunks hold
  the same data as their source rows, under the same RLS (ADR 0013 section 4, "Not under the
  envelope").

Expand only: two new owned tables (forced RLS, the ``owner_isolation`` policy), no change to an
existing one, no data moved. Account deletion removes both through the ``owner_id`` cascade. The
vocabularies are written out here on purpose; ``ownvitals.reports.tables`` holds the same lists
and ``tests/test_reports_export_db.py`` compares them with the check constraints.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0022_exports"
down_revision: str | Sequence[str] | None = "0021_ai_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXPORTS = """
CREATE TABLE public.exports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    kind text NOT NULL CONSTRAINT exports_kind CHECK (kind IN ('data', 'doctor_pdf')),
    state text NOT NULL DEFAULT 'building'
        CONSTRAINT exports_state CHECK (state IN ('building', 'ready', 'failed', 'expired')),
    job_id uuid,
    failure text CONSTRAINT exports_failure
        CHECK (failure IS NULL OR failure IN ('export_too_large', 'build_failed')),
    largest text CONSTRAINT exports_largest CHECK (largest IS NULL OR largest ~ '^[a-z_]{1,40}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    expires_at timestamptz,
    size bigint CONSTRAINT exports_size CHECK (size IS NULL OR size >= 0),
    sha256 bytea CONSTRAINT exports_sha256 CHECK (sha256 IS NULL OR octet_length(sha256) = 32),
    download_attempts integer NOT NULL DEFAULT 0
        CONSTRAINT exports_download_attempts CHECK (download_attempts BETWEEN 0 AND 3),
    download_count integer NOT NULL DEFAULT 0
        CONSTRAINT exports_download_count CHECK (download_count BETWEEN 0 AND 1),
    CONSTRAINT exports_ready CHECK (
        state <> 'ready'
        OR (ready_at IS NOT NULL AND expires_at IS NOT NULL AND size IS NOT NULL
            AND sha256 IS NOT NULL)
    ),
    CONSTRAINT exports_failed CHECK ((state = 'failed') = (failure IS NOT NULL)),
    CONSTRAINT exports_owner_id_id_key UNIQUE (owner_id, id)
)"""
# One build in progress per owner and kind.
_ONE_BUILDING = (
    "CREATE UNIQUE INDEX exports_one_building ON public.exports (owner_id, kind) "
    "WHERE state = 'building'"
)

_CHUNKS = """
CREATE TABLE public.export_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    export_id uuid NOT NULL,
    seq integer NOT NULL CONSTRAINT export_chunks_seq CHECK (seq >= 0),
    bytes bytea NOT NULL
        CONSTRAINT export_chunks_bytes CHECK (octet_length(bytes) BETWEEN 1 AND 4194304),
    CONSTRAINT export_chunks_export FOREIGN KEY (owner_id, export_id)
        REFERENCES public.exports (owner_id, id) ON DELETE CASCADE,
    CONSTRAINT export_chunks_owner_export_seq_key UNIQUE (owner_id, export_id, seq),
    CONSTRAINT export_chunks_owner_id_id_key UNIQUE (owner_id, id)
)"""


def upgrade() -> None:
    op.execute(_EXPORTS)
    op.execute(_ONE_BUILDING)
    enable_owner_rls("exports")
    op.execute(_CHUNKS)
    enable_owner_rls("export_chunks")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.export_chunks")
    op.execute("DROP TABLE IF EXISTS public.exports")
