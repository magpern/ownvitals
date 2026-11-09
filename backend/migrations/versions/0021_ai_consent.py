"""AI consent and the conversation history (ADR 0014 section 3; #206)

Revision ID: 0021_ai_consent
Revises: 0020_notify_holds
Create Date: 2026-11-09

- ``ai_consents`` (owned): one row per consent given: the version of the consent text, the
  SHA-256 of the exact text shown, when it was given and, once withdrawn, when. At most one row
  per owner is live (``withdrawn_at IS NULL``); a withdrawn row stays as the record of the
  consent and its withdrawal (it holds no content). A reply or digest is stored only after
  reading the live row ``FOR SHARE``; withdrawal updates that row first, so the two serialise on
  it (ADR 0014 section 3).
- ``ai_conversations`` and ``ai_messages`` (owned): the conversation history that withdrawal
  deletes. A message belongs to a conversation of the same owner (a composite foreign key with
  ``ON DELETE CASCADE``). Its text is untrusted model output or the user's own text, bounded in
  length; no tool result, prompt or identifier is stored here.

Expand only: three new owned tables (forced RLS, the ``owner_isolation`` policy), no change to an
existing one, no data moved. Account deletion removes all three through the ``owner_id``
cascade. The vocabularies are written out here on purpose; ``ownvitals.ai.tables`` holds the
same lists and ``tests/test_ai_consent_db.py`` compares them with the check constraints.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0021_ai_consent"
down_revision: str | Sequence[str] | None = "0020_notify_holds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSENTS = """
CREATE TABLE public.ai_consents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    consent_version integer NOT NULL
        CONSTRAINT ai_consents_version CHECK (consent_version BETWEEN 1 AND 10000),
    locale text NOT NULL CONSTRAINT ai_consents_locale CHECK (locale ~ '^[a-z]{2}$'),
    text_sha256 bytea NOT NULL
        CONSTRAINT ai_consents_text_sha256 CHECK (octet_length(text_sha256) = 32),
    given_at timestamptz NOT NULL DEFAULT now(),
    withdrawn_at timestamptz,
    CONSTRAINT ai_consents_withdrawn CHECK (withdrawn_at IS NULL OR withdrawn_at >= given_at),
    CONSTRAINT ai_consents_owner_id_id_key UNIQUE (owner_id, id)
)"""
# At most one live consent per owner.
_LIVE = (
    "CREATE UNIQUE INDEX ai_consents_live ON public.ai_consents (owner_id) "
    "WHERE withdrawn_at IS NULL"
)

_CONVERSATIONS = """
CREATE TABLE public.ai_conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_conversations_updated CHECK (updated_at >= created_at),
    CONSTRAINT ai_conversations_owner_id_id_key UNIQUE (owner_id, id)
)"""

_MESSAGES = """
CREATE TABLE public.ai_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL,
    role text NOT NULL CONSTRAINT ai_messages_role CHECK (role IN ('user', 'assistant')),
    content text NOT NULL
        CONSTRAINT ai_messages_content CHECK (char_length(content) BETWEEN 1 AND 16000),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_messages_conversation FOREIGN KEY (owner_id, conversation_id)
        REFERENCES public.ai_conversations (owner_id, id) ON DELETE CASCADE,
    CONSTRAINT ai_messages_owner_id_id_key UNIQUE (owner_id, id)
)"""
_MESSAGES_ORDER = (
    "CREATE INDEX ai_messages_conversation_order ON public.ai_messages "
    "(owner_id, conversation_id, created_at)"
)


def upgrade() -> None:
    op.execute(_CONSENTS)
    op.execute(_LIVE)
    enable_owner_rls("ai_consents")
    op.execute(_CONVERSATIONS)
    enable_owner_rls("ai_conversations")
    op.execute(_MESSAGES)
    op.execute(_MESSAGES_ORDER)
    enable_owner_rls("ai_messages")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.ai_messages")
    op.execute("DROP TABLE IF EXISTS public.ai_conversations")
    op.execute("DROP TABLE IF EXISTS public.ai_consents")
