"""What the doctor PDF job reads (ADR 0013 section 6), all for the job's own user.

- **Daily values** of the chosen metrics, in the job's fenced session under forced RLS (the
  statement filters ``owner_id`` too), from 90 days before the period for the typical range. A
  day is ``manual`` when one of its readings has ``attrib`` 2 or 4; a day that only unconfirmed
  readings (``attrib`` 1) feed is left out, though the daily values never use them.
- **Findings** only through ``views.findings_read`` (consumer ``doctor_pdf``: a hidden finding
  is left out), the device tier only, rules E1 to E3, never an experimental rule, and only those
  whose window meets the period.
- **ECG recordings** through ``views.ecg_read``: Withings' values and the stored signal.
- **The birth year** through ``views.profile_store``.
Nothing here reads an estimate, a score, a trend finding or AI text.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.jobs.run import JobRun
from ownvitals.reports.doctor_model import BAND_DAYS, FINDING_TEXT, DailyRow, EcgNote, FindingLine
from ownvitals.reports.ecg_facts import facts_for
from ownvitals.reports.ecg_layout import ReportFacts
from ownvitals.reports.ecg_pdf import local_time_label
from ownvitals.views.ecg_read import read_recording
from ownvitals.views.findings_models import EXPERIMENTAL_RULES, FindingsQuery
from ownvitals.views.findings_read import read_findings
from ownvitals.views.profile_store import read_profile

MANUAL_ATTRIB: Final = (2, 4)  # entered by hand (ADR 0013 section 6)
DEVICE_RULES: Final = frozenset(FINDING_TEXT) - EXPERIMENTAL_RULES

_DAILY = text(
    "SELECT d.metric_key, d.day, d.value, d.method, d.n, d.min, d.max, d.mad, d.algo_version, "
    "coalesce(a.manual, false) AS manual "
    "FROM metric_daily AS d CROSS JOIN LATERAL ("
    "SELECT bool_or(g.attrib = ANY(:manual)) AS manual, bool_and(g.attrib = 1) AS only_unconfirmed "
    "FROM measure_groups AS g WHERE g.owner_id = d.owner_id AND g.grpid = ANY(d.source_groups) "
    "AND g.deleted_at IS NULL) AS a "
    "WHERE d.owner_id = :owner AND d.metric_key = ANY(CAST(:keys AS text[])) "
    "AND d.day BETWEEN :lo AND :hi AND NOT coalesce(a.only_unconfirmed, false) "
    "ORDER BY d.metric_key, d.day"
)


async def read_daily(
    db: AsyncSession, owner: UUID, keys: Sequence[str], start: date, end: date
) -> dict[str, list[DailyRow]]:
    params = {
        "owner": owner,
        "keys": list(keys),
        "lo": start - timedelta(days=BAND_DAYS),
        "hi": end,
        "manual": list(MANUAL_ATTRIB),
    }
    found: dict[str, list[DailyRow]] = defaultdict(list)
    for row in (await db.execute(_DAILY, params)).mappings():
        found[row["metric_key"]].append(
            DailyRow(
                day=row["day"],
                value=row["value"],
                method=row["method"],
                n=row["n"],
                minimum=row["min"],
                maximum=row["max"],
                mad=row["mad"],
                algo_version=row["algo_version"],
                manual=bool(row["manual"]),
            )
        )
    return found


async def read_birth_year(job: JobRun, settings: Settings) -> int | None:
    profile = await read_profile(job.factory, job.ctx, settings)
    return profile.birth_year


async def read_device_findings(
    job: JobRun, settings: Settings, start: date, end: date
) -> tuple[FindingLine, ...]:
    read = await read_findings(
        job.factory,
        job.ctx,
        settings,
        consumer="doctor_pdf",
        query=FindingsQuery(tier="device"),
    )
    lines = [
        FindingLine(f.rule_id, FINDING_TEXT[f.rule_id], f.window_start, f.window_end)
        for f in read.findings
        if f.tier == "device"
        and f.rule_id in DEVICE_RULES
        and f.window_start <= end
        and f.window_end >= start
    ]
    return tuple(sorted(lines, key=lambda line: (line.first, line.rule_id)))


async def read_strips(
    job: JobRun, recording_ids: Sequence[UUID]
) -> tuple[list[ReportFacts], list[EcgNote]]:
    """The chosen recordings in the order given: the facts of each that has a signal, a note
    for each that has none (or is not the user's: it reads as missing, like any other)."""
    facts: list[ReportFacts] = []
    notes: list[EcgNote] = []
    for recording_id in recording_ids:
        r = await read_recording(job.factory, job.ctx, recording_id)
        if r is None:
            notes.append(EcgNote(str(recording_id)[:8], "the recording was not found"))
        elif r.leads is None or r.sampling_hz is None:
            notes.append(EcgNote(local_time_label(r.recorded_at, r.source_tz), "no signal stored"))
        else:
            facts.append(facts_for(r, r.leads, r.sampling_hz))
    return facts, notes
