"""``ownvitals analysis check --user <uuid>``: the owner's reproducibility check (#187; M4 done
when "reproducible on your data", PLAN §11).

It reads one account under forced RLS, in one read-only user session, recomputes in memory

* the daily values from the stored observations (``daily.compute``, with the profile's day
  setting) and compares them with the stored ``metric_daily`` rows, by content;
* the rule evaluation from the same inputs the worker reads (``findings_store.gather`` and
  ``evaluate``) and compares the episodes with the stored rows of the rules it owns, by the diff
  the worker would apply (``findings_store.diff_rows``): a row the worker would insert, update,
  end or delete is a mismatch.

It writes nothing. Like the probe (PLAN-operations §10.4) it prints **counts, versions, rule ids
and states only**: never a value, a date, a timestamp, a subject or an id. A failure names the
exception's class only, so a database error cannot echo a row. Exit 0 when everything matches,
1 on a mismatch or a refusal.

Time matters to the rules (today, Q1's 14 days): run it after the day's evaluation has run, or a
day's change in ``today`` shows up as a mismatch the next pass would write.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    user_session,
    verify_database_role,
)
from ownvitals.derived import daily
from ownvitals.derived.daily_db import read_day_tz, read_observations
from ownvitals.derived.evaluate import OWNED_RULES, RULES_VERSION, evaluate
from ownvitals.derived.findings_store import Gathered, diff_rows, gather
from ownvitals.jobs.issuer import owner_context

_DAILY = text(
    "SELECT metric_key, day, method, value, morning, mean, min, max, mad, n, source_groups "
    "FROM metric_daily WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[]))"
)
_STORED = text(
    "SELECT id, rule_id, rule_version, tier, severity, confidence, data_quality, origin, "
    "metric_keys, subject, window_start, window_end, reference_day, evidence, status, "
    "first_seen, last_seen, ended_at FROM findings WHERE owner_id = :o "
    "AND rule_id = ANY(CAST(:rules AS text[]))"
)


@dataclass(frozen=True, slots=True)
class Report:
    """The command's stdout, one line each, and whether anything differed."""

    lines: tuple[str, ...]
    failed: bool


def _stored_daily(row: Any) -> tuple[Any, ...]:
    return (
        row.method,
        row.value,
        row.morning,
        row.mean,
        row.min,
        row.max,
        row.mad,
        row.n,
        tuple(row.source_groups),
    )


def _computed_daily(row: daily.DailyRow) -> tuple[Any, ...]:
    return (
        row.method,
        row.value,
        row.morning,
        row.mean,
        row.minimum,
        row.maximum,
        row.mad,
        row.n,
        row.source_groups,
    )


async def _daily_lines(db: AsyncSession, owner: UUID) -> tuple[list[str], bool]:
    day_tz = await read_day_tz(db, owner)
    computed = daily.compute(await read_observations(db, owner), day_tz=day_tz)
    stored = {
        (r.metric_key, r.day): _stored_daily(r)
        for r in (await db.execute(_DAILY, {"o": owner, "keys": list(daily.KEYS)})).all()
    }
    wanted = {row.key: _computed_daily(row) for row in computed.rows}
    matching = sum(1 for key, value in wanted.items() if stored.get(key) == value)
    differ = matching != len(wanted) or len(stored) != len(wanted)
    return [
        f"daily_version {daily.ALGO_VERSION}",
        f"day_assignment {'fixed_tz' if day_tz else 'reading_tz'}",
        f"daily_rows stored {len(stored)} computed {len(wanted)} matching {matching}",
    ], differ


async def _rule_lines(
    db: AsyncSession, owner: UUID, settings: Settings, now: datetime
) -> tuple[list[str], bool]:
    lines = [f"rules_version {RULES_VERSION}"]
    read = await gather(db, owner, settings=settings, now=now)
    if not isinstance(read, Gathered):
        lines.append(f"rule_inputs {read.status}")
        # Nothing imported is a match (no rows either way); behind is not checkable yet.
        return lines, read.status != "nothing"
    evaluation = evaluate(read.inputs)
    stored = list((await db.execute(_STORED, {"o": owner, "rules": list(OWNED_RULES)})).all())
    diff = diff_rows(stored, evaluation, read.inputs.today, owner, read.revision, read.fingerprint)
    changes = {
        "insert": len(diff.inserts),
        "update": len(diff.updates),
        "end": len(diff.ends) + len(diff.closes),
        "delete": len(diff.deletes),
    }
    computed = len(evaluation.episodes) + sum(1 for r in evaluation.q1 if r.finding is not None)
    lines.append("rule_inputs current")
    lines.append(f"experimental {'on' if read.inputs.experimental else 'off'}")
    for rule in sorted(evaluation.evaluated):
        lines.append(f"evaluated {rule} {evaluation.evaluated[rule]}")
    by_status: dict[str, int] = {}
    for row in stored:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    lines.append(
        f"rule_rows stored {len(stored)} computed {computed} "
        f"active {by_status.get('active', 0)} ended {by_status.get('ended', 0)}"
    )
    lines.append(" ".join(f"{name} {count}" for name, count in changes.items()))
    return lines, any(changes.values())


async def check_owner(
    db: AsyncSession, owner: UUID, settings: Settings, *, now: datetime | None = None
) -> Report:
    """The report for one owner, from one session (see the module docstring)."""
    moment = now or datetime.now(UTC)
    daily_lines, daily_differs = await _daily_lines(db, owner)
    rule_lines, rules_differ = await _rule_lines(db, owner, settings, moment)
    failed = daily_differs or rules_differ
    return Report(
        (*daily_lines, *rule_lines, f"result {'mismatch' if failed else 'match'}"), failed
    )


def add_analysis_subcommands(sub: Any) -> None:
    """``analysis check``. Installed by the operator CLI."""
    analysis = sub.add_parser("analysis", help="analysis checks for one user").add_subparsers(
        dest="action", required=True
    )
    check = analysis.add_parser(
        "check", help="recompute daily values and episodes and compare them (counts only)"
    )
    check.add_argument("--user", required=True, help="the account to read, under forced RLS")


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Read ``--user`` under forced RLS and print the report. Exit 1 on a mismatch or failure."""
    if args.action != "check":
        print("error: unknown analysis action", file=err)
        return 2
    try:
        owner = UUID(str(args.user))
    except ValueError:
        print("error: the user id is not a uuid", file=err)
        return 1
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        factory = create_session_factory(engine)
        async with user_session(factory, owner_context(owner)) as db:
            report = await check_owner(db, owner, settings)
    except DatabaseRoleError as refused:  # names roles and objects only
        print(f"error: {refused}", file=err)
        return 1
    except (SQLAlchemyError, ValueError, TypeError) as exc:  # the class only, never a row
        print(f"error: the check failed ({type(exc).__name__})", file=err)
        return 1
    finally:
        await engine.dispose()
    for line in report.lines:
        print(line, file=out)
    return 1 if report.failed else 0
