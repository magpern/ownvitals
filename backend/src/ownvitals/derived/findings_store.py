"""The database side of the rule evaluation (#187): read the inputs, evaluate, verify, write.

``evaluate_owner`` is one owner's pass, in two transactions opened by ``open_tx`` (a job passes
its fenced transaction, a test a user session). Every statement filters ``owner_id`` itself as
well as running under forced RLS (ADR 0002).

1. **Read** (``gather``) the ``measures`` revision first, then the profile's day setting, the
   observations, the stored ``metric_daily`` rows, the group ``attrib`` of the nerve readings,
   Q1's state records, the owner-only switch and the ECG inputs (``findings_ecg``: the
   recordings' Withings fields and their A7 groups, the profile's sex). ``nothing`` without a
   revision row (nothing imported, or purged). ``behind`` when an observation read carries
   another revision (the normaliser has not caught up) or the daily rows are not those of these
   observations and this day setting (the daily pass has not caught up; it queues this pass when
   it has).
2. **Evaluate** (``derived.evaluate``, pure).
3. **Write**, in one transaction: take the owner's normalisation lock (namespace 6, ADR 0005 §9:
   the lock the normaliser's and the daily pass's writes take), lock the revision row ``FOR
   SHARE`` (an ingest bump or the purge of imported data waits for this transaction), and
   **gather the inputs again** after both locks: the revision and the fingerprint must be the
   ones read, else nothing is written (``stale``). Same discipline as ``daily_db`` (the purge
   race of the review of #225).

**The diff** (ADR 0010 §4) touches only the rows of the rules this writer owns
(``evaluate.OWNED_RULES``, E1 to E4 and E6 included since #191); any other rule id (E5 is
deferred) is never read, changed or deleted.

* Replayed rules: a stored episode and a computed one with the same rule, subject and reference
  day are the same episode. It keeps its id, ``first_seen``, ``origin``, ``notify_state``,
  ``notified_at`` and ``seen_at``; the rest is updated when it differs. A computed episode with
  no stored match is inserted, with ``origin = origin(window_end, today)`` (computed once, at this
  first write) and ``notify_state`` ``pending`` exactly when that origin is ``live``. A stored
  episode no recompute produces (its data changed, or the owner-only switch is off) is deleted.
* Q1: today's result is applied to the stored active episode of its rule and subject by
  ``advance_simple``: fire and none active, insert (live, so ``pending``); fire while active,
  update (the reference day and origin stay); no fire, or a subject no longer reported (a device
  or a stream gone), end it. Ended Q1 episodes are history and never touched.
* Statements run in the order deletes, ends, other updates, inserts, so the one-active-episode
  index (``findings_active_episode``) is never violated mid-diff.

``first_seen``, ``last_seen`` and ``ended_at`` are timestamps in the schema; the pass stores the
evaluation day of the first firing, the latest firing and the end at 00:00 UTC, so a recompute
of the same data gives byte-identical rows. Provenance: ``input_revision`` is the ``measures``
counter (the one stream the v1 rules read, through observations and daily values; Q1's state
records are not a ``data_revisions`` stream) and ``input_fingerprint`` is
``evaluate.fingerprint`` of the inputs. Every owned row the pass re-derives carries both; an
ended Q1 episode keeps the provenance it ended with.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.db.queue_locks import NORMALISATION
from ownvitals.derived.evaluate import OWNED_RULES, Q1_RULES, Evaluation, evaluate
from ownvitals.derived.findings_diff import content, day_start, diff_rows
from ownvitals.derived.findings_gather import Gathered, Outcome, gather, read_revision

CHUNK: Final = 500

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_LOCK = text("SELECT pg_catalog.pg_advisory_xact_lock(:ns, pg_catalog.hashtext(CAST(:o AS text)))")
_COLUMNS = (
    "id, rule_id, rule_version, tier, severity, confidence, data_quality, origin, metric_keys, "
    "subject, window_start, window_end, reference_day, evidence, status, first_seen, last_seen, "
    "ended_at"
)
_STORED = text(
    f"SELECT {_COLUMNS} FROM findings WHERE owner_id = :o "  # noqa: S608 - constant columns
    "AND rule_id = ANY(CAST(:rules AS text[]))"
)
_INSERT = text(
    "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
    "data_quality, origin, metric_keys, subject, window_start, window_end, reference_day, "
    "evidence, input_revision, input_fingerprint, status, first_seen, last_seen, ended_at, "
    "notify_state) VALUES (:o, :rule_id, :rule_version, :tier, :severity, :confidence, "
    "CAST(:data_quality AS jsonb), :origin, CAST(:metric_keys AS text[]), :subject, "
    ":window_start, :window_end, :reference_day, CAST(:evidence AS jsonb), :r, :f, :status, "
    ":first_seen, :last_seen, :ended_at, :notify_state)"
)
_UPDATE = text(
    "UPDATE findings SET rule_version = :rule_version, tier = :tier, severity = :severity, "
    "confidence = :confidence, data_quality = CAST(:data_quality AS jsonb), "
    "metric_keys = CAST(:metric_keys AS text[]), window_start = :window_start, "
    "window_end = :window_end, evidence = CAST(:evidence AS jsonb), input_revision = :r, "
    "input_fingerprint = :f, status = :status, last_seen = :last_seen, ended_at = :ended_at "
    "WHERE owner_id = :o AND id = :id"
)
_DELETE = text("DELETE FROM findings WHERE owner_id = :o AND id = :id")
_CLOSE = text(
    "UPDATE findings SET status = 'ended', ended_at = :ended_at "
    "WHERE owner_id = :o AND id = :id AND status = 'active'"
)
# Moves the provenance of every owned row the pass re-derived and left unchanged; an ended Q1
# episode (history, not re-derived) keeps its own.
_PROVENANCE = text(
    "UPDATE findings SET input_revision = :r, input_fingerprint = :f WHERE owner_id = :o "
    "AND rule_id = ANY(CAST(:rules AS text[])) "
    "AND (status = 'active' OR rule_id <> ALL(CAST(:q1 AS text[]))) "
    "AND (input_revision, input_fingerprint) IS DISTINCT FROM (CAST(:r AS bigint), "
    "CAST(:f AS bytea))"
)


async def _chunks(db: AsyncSession, statement: Any, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), CHUNK):
        await db.execute(statement, rows[start : start + CHUNK])


async def _write(
    db: AsyncSession,
    owner: UUID,
    read: Gathered,
    evaluation: Evaluation,
    *,
    settings: Settings,
    now: datetime,
) -> Outcome:
    await db.execute(_LOCK, {"ns": NORMALISATION, "o": owner})
    if await read_revision(db, owner, lock=True) != read.revision:
        return Outcome("stale")
    again = await gather(db, owner, settings=settings, now=now)
    if not isinstance(again, Gathered) or again.fingerprint != read.fingerprint:
        return Outcome("stale")
    stored = list((await db.execute(_STORED, {"o": owner, "rules": list(OWNED_RULES)})).all())
    r, f = read.revision, read.fingerprint
    diff = diff_rows(stored, evaluation, read.inputs.today, owner, r, f)
    await _chunks(db, _DELETE, diff.deletes)
    await _chunks(db, _CLOSE, diff.closes)
    await _chunks(db, _UPDATE, diff.ends)
    await _chunks(db, _UPDATE, diff.updates)
    await _chunks(db, _INSERT, diff.inserts)
    moved = await db.execute(
        _PROVENANCE, {"o": owner, "rules": list(OWNED_RULES), "q1": list(Q1_RULES), "r": r, "f": f}
    )
    ended = len(diff.ends) + len(diff.closes)
    changed = len(diff.inserts) + ended + len(diff.updates) + len(diff.deletes)
    restamped = int(getattr(moved, "rowcount", 0) or 0)
    return Outcome(
        "written" if changed or restamped else "current",
        inserted=len(diff.inserts),
        updated=len(diff.updates),
        ended=ended,
        deleted=len(diff.deletes),
        pending=sum(1 for p in diff.inserts if p["notify_state"] == "pending"),
        evaluated=dict(evaluation.evaluated),
    )


async def evaluate_owner(
    open_tx: OpenTx,
    owner: UUID,
    *,
    settings: Settings,
    now: datetime | None = None,
    between: Callable[[], Awaitable[None]] | None = None,
) -> Outcome:
    """One owner's pass (see the module docstring). ``between`` runs after the read and before
    the write; a job passes its checkpoint, a test interleaves a purge or an ingest."""
    moment = now or datetime.now(UTC)
    async with open_tx() as db:
        read = await gather(db, owner, settings=settings, now=moment)
    if isinstance(read, Outcome):
        return read
    evaluation = evaluate(read.inputs)
    if between is not None:
        await between()
    async with open_tx() as db:
        return await _write(db, owner, read, evaluation, settings=settings, now=moment)


__all__ = [
    "Gathered",
    "Outcome",
    "content",
    "day_start",
    "diff_rows",
    "evaluate_owner",
    "gather",
]
