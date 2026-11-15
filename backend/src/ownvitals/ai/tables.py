"""The consent and conversation tables of migration ``0021_ai_consent`` (ADR 0014 section 3; #206)
and the digest tables of ``0026_ai_digest`` (issue 209).

All three are owned: ``owner_id`` cascading from ``users``, forced RLS and the
``owner_isolation`` policy. ``ai_messages`` belongs to a conversation of the same owner through a
composite foreign key that cascades. The vocabularies are the migration's check constraints,
copied there, not imported; ``tests/test_ai_consent_db.py`` holds both equal.

Narrow Core handles: the columns the consent store and the purge name, nothing else.
"""

from typing import Final, Literal, get_args

from sqlalchemy import Column, Date, DateTime, Integer, LargeBinary, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import UUID

MessageRole = Literal["user", "assistant"]
MESSAGE_ROLES: Final[tuple[str, ...]] = get_args(MessageRole)
MESSAGE_MAX_CHARS: Final = 16_000
DigestStatus = Literal["ready", "skipped_budget", "unavailable"]
DIGEST_STATUSES: Final[tuple[str, ...]] = get_args(DigestStatus)
DIGEST_MAX_CHARS: Final = 16_000

# The owned tables of migration 0021, children before parents.
AI_CONSENT_OWNED: Final = ("ai_messages", "ai_conversations", "ai_consents")
# The owned tables of migration 0026 (issue 209).
AI_DIGEST_OWNED: Final = ("ai_digests", "ai_digest_prefs")
# What withdrawal deletes (ADR 0014 section 3), children before parents: the conversation
# history, the stored digests and the digest opt-in (consenting again starts with it off).
# ``ai_consents`` stays as the record of the consent and its withdrawal; so do the spend tables
# (``ai.budget``).
HISTORY: Final = ("ai_messages", "ai_conversations", "ai_digests", "ai_digest_prefs")

metadata = MetaData()

ai_consents = Table(
    "ai_consents",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("consent_version", Integer, nullable=False),
    Column("locale", Text, nullable=False),
    Column("text_sha256", LargeBinary, nullable=False),
    Column("given_at", DateTime(timezone=True), nullable=False),
    Column("withdrawn_at", DateTime(timezone=True)),
)

ai_conversations = Table(
    "ai_conversations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
)

ai_messages = Table(
    "ai_messages",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("conversation_id", UUID(as_uuid=True), nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
)

ai_digests = Table(
    "ai_digests",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("week_start", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("content", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

ai_digest_prefs = Table(
    "ai_digest_prefs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("opted_in_at", DateTime(timezone=True), nullable=False),
)

HISTORY_TABLES: Final = (ai_messages, ai_conversations, ai_digests, ai_digest_prefs)
