"""``notify_holds``: ADR 0006 section 11's notification hold, one row per held E4/E6 finding
(migration ``0020_notify_holds``, #195); ``email_prefs``: the email preference per suppressible
mail kind (migration ``0029_email_prefs``, ADR 0017, #459).

Owned: ``owner_id`` cascading from ``users``, forced RLS and the ``owner_isolation`` policy.
``finding_id`` is not a foreign key, so a hold outlives a withdrawn (deleted) finding and records
``withdrawn``. The vocabularies are the migration's check constraints, copied there, not
imported; ``tests/test_notify_hold_db.py`` holds both equal.
"""

from typing import Final, Literal, get_args

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

# How a hold ended (ADR 0006 §13's "gate result, no link possible, bound"; #195): ``linked``
# (every trigger group linked, or can no longer be, and at least one is, each with a current
# ``estimated``), ``capability_unsupported`` and ``ecg_sync_no_match`` (no link possible: the
# connection's ``ecg`` capability, or a successful ``ecg`` sync that started after the revision
# found no match), ``no_trigger`` (no trigger group with a live measure group at all),
# ``timeout`` (the bound) and ``withdrawn`` (the finding went during the hold).
Reason = Literal[
    "linked", "capability_unsupported", "ecg_sync_no_match", "no_trigger", "timeout", "withdrawn"
]
# What the end did: sent (with or without section 11's "not evaluated by ownvitals"), hidden by
# the gate (nothing sent, ever), or nothing (withdrawn, or the tier is not one the user notifies).
HoldOutcome = Literal["sent", "sent_not_evaluated", "hidden", "none"]

REASONS: Final[tuple[str, ...]] = get_args(Reason)
HOLD_OUTCOMES: Final[tuple[str, ...]] = get_args(HoldOutcome)
HELD_RULES: Final = ("E4", "E6")
NOTIFY_OWNED: Final = ("notify_holds", "email_prefs")

metadata = MetaData()

notify_holds = Table(
    "notify_holds",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "owner_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    ),
    Column("finding_id", UUID(as_uuid=True), nullable=False),
    Column("rule_id", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("revision_at", DateTime(timezone=True), nullable=False),
    Column("revision_seen", DateTime(timezone=True), nullable=False),
    Column(
        "revision_groups",
        ARRAY(BigInteger),
        nullable=False,
        server_default=text("'{}'::bigint[]"),
    ),
    Column("ended_at", DateTime(timezone=True)),
    Column("reason", Text),
    Column("outcome", Text),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", "finding_id", name="notify_holds_finding_key"),
)

# Owned: at most one row per owner and suppressible kind; no row is "on, generation 0". The
# generation counts every change of ``enabled`` (ADR 0017: an unsubscribe link binds it).
email_prefs = Table(
    "email_prefs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "owner_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    ),
    Column("kind", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("generation", BigInteger, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", "kind", name="email_prefs_owner_kind"),
)
