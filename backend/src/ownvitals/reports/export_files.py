"""The files of the export archive, one entry at a time (ADR 0013 sections 1 and 2; #211).

Each writer here fills one or two entries of an ``Archive`` from rows it is handed or reads in
pages through the job (``ExportJob``): the account's two files, every ``GENERIC`` table, the ECG
recordings with their signal, the findings with their overlay state, the up-to-date estimates and
the manifest. JSON entries are arrays of objects (``export_format.json_row``), CSV entries the same
rows flattened under a header.

The builder (``ownvitals.reports.export``) owns the job, the fence and the chunk writes; between
pages it calls ``ExportJob.flush``, which stores the archive's full chunks and checks the lease.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.reports.export_allowlist import (
    ECG_TABLE,
    ESTIMATES_FILE,
    ESTIMATES_LABEL,
    FINDINGS_TABLE,
    SPECIAL_COLUMNS,
    TableSpec,
)
from ownvitals.reports.export_archive import Archive
from ownvitals.reports.export_ecg import COLUMNS as ECG_COLUMNS
from ownvitals.reports.export_format import csv_header, csv_lines, json_row, real_value

Page = Callable[[AsyncSession, UUID | None], Awaitable[Sequence[Mapping[str, Any]]]]

OVERLAY_SHOWN: Final = "shown"
OVERLAY_HIDDEN: Final = "hidden_gate_failed"
OVERLAY_NOT_EVALUATED: Final = "not_evaluated"
FINDING_COLUMNS: Final = (*SPECIAL_COLUMNS[FINDINGS_TABLE], "overlay_state")
ESTIMATE_COLUMNS: Final = (
    "recording_id", "source", "gate", "reasons", "windows", "rpeaks", "hr_bpm", "hrv",
)  # fmt: skip
SIGNAL_COLUMNS: Final = ("recording_id", "lead", "index", "microvolts")
_ECG_CSV: Final = tuple(c for c in ECG_COLUMNS if c != "signal")


class ExportJob(Protocol):
    """What a page writer needs of the running build."""

    archive: Archive

    async def page(self, read: Page, after: UUID | None) -> Sequence[Mapping[str, Any]]:
        """One page through the fence, after storing the archive's full chunks."""
        ...


class _Json:
    """An open JSON array entry: ``[``, the rows separated by ``,``, ``]``."""

    def __init__(self, archive: Archive, stem: str, *, label: str | None = None) -> None:
        self.archive = archive
        self.first = True
        archive.begin(f"data/{stem}.json", stem, label=label)
        archive.write("[")

    def rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        parts: list[str] = []
        count = 0
        for row in rows:
            parts.append(("\n" if self.first else ",\n") + json_row(row))
            self.first = False
            count += 1
        if parts:
            self.archive.write("".join(parts), rows=count)

    def end(self) -> None:
        self.archive.write("\n]\n")
        self.archive.end()


def _csv_begin(archive: Archive, stem: str, columns: Sequence[str], label: str | None) -> None:
    archive.begin(f"csv/{stem}.csv", stem, label=label)
    archive.write(csv_header(columns))


def _csv_rows(archive: Archive, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    if rows:
        archive.write(csv_lines([row[c] for c in columns] for row in rows), rows=len(rows))


def whole(
    archive: Archive,
    stem: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str | None = None,
) -> None:
    """Both entries of a table that is already in memory (the account's, findings, estimates)."""
    entry = _Json(archive, stem, label=label)
    entry.rows({c: row[c] for c in columns} for row in rows)
    entry.end()
    _csv_begin(archive, stem, columns, label)
    _csv_rows(archive, columns, rows)
    archive.end()


def _with_value(spec: TableSpec, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(row) for row in rows]
    if spec.real_value:
        for row in out:
            row["value"] = real_value(row["value_raw"], row["unit"])
    return out


async def paged(job: ExportJob, spec: TableSpec, read: Page) -> None:
    """A ``GENERIC`` table in pages: its JSON entry, then a second pass for its CSV entry."""
    columns = (*spec.columns, "value") if spec.real_value else spec.columns
    archive = job.archive
    entry = _Json(archive, spec.file)
    after: UUID | None = None
    while rows := await job.page(read, after):
        entry.rows(_with_value(spec, rows))
        after = rows[-1]["id"]
    entry.end()
    _csv_begin(archive, spec.file, columns, None)
    after = None
    while rows := await job.page(read, after):
        _csv_rows(archive, columns, _with_value(spec, rows))
        after = rows[-1]["id"]
    archive.end()


async def recordings(job: ExportJob, read: Page) -> list[UUID]:
    """The ECG recordings: JSON with the signal's leads, CSV without, and the long signal table
    (``recording_id, lead, index, microvolts``). Returns the recordings' ids."""
    archive = job.archive
    ids: list[UUID] = []
    entry = _Json(archive, ECG_TABLE)
    after: UUID | None = None
    while rows := await job.page(read, after):
        entry.rows(rows)
        ids.extend(row["id"] for row in rows)
        after = rows[-1]["id"]
    entry.end()
    _csv_begin(archive, ECG_TABLE, _ECG_CSV, None)
    after = None
    while rows := await job.page(read, after):
        _csv_rows(archive, _ECG_CSV, rows)
        after = rows[-1]["id"]
    archive.end()
    _csv_begin(archive, "ecg_signal", SIGNAL_COLUMNS, None)
    after = None
    while rows := await job.page(read, after):
        for row in rows:
            signal = row["signal"] or []
            count = sum(len(lead) for lead in signal)
            if count:
                archive.write(
                    csv_lines(
                        (row["id"], lead_no, index, value)
                        for lead_no, lead in enumerate(signal)
                        for index, value in enumerate(lead)
                    ),
                    rows=count,
                )
        after = rows[-1]["id"]
    archive.end()
    return ids


def overlay_state(hidden: bool, not_evaluated: bool) -> str:
    """ADR 0006 section 11's overlay state of one finding, as the export names it."""
    if hidden:
        return OVERLAY_HIDDEN
    if not_evaluated:
        return OVERLAY_NOT_EVALUATED
    return OVERLAY_SHOWN


def finding_rows(views: Sequence[Any]) -> list[dict[str, Any]]:
    """Every finding the read function gave the ``export`` consumer, with its overlay state."""
    rows: list[dict[str, Any]] = []
    for view in views:
        row = {c: getattr(view, c) for c in SPECIAL_COLUMNS[FINDINGS_TABLE]}
        row["overlay_state"] = overlay_state(view.hidden, view.not_evaluated)
        rows.append(row)
    return rows


def estimate_rows(estimates: Mapping[UUID, Any]) -> list[dict[str, Any]]:
    """Section 9's up-to-date estimates; an empty view (stale or none) is left out, not marked."""
    rows: list[dict[str, Any]] = []
    for recording_id in sorted(estimates, key=str):
        view = estimates[recording_id]
        shown = (view.gate, view.reasons, view.windows, view.rpeaks, view.hr_bpm, view.hrv)
        if all(v is None or v == () for v in shown):
            continue
        rows.append(
            {
                "recording_id": recording_id,
                "source": ESTIMATES_LABEL,
                "gate": view.gate,
                "reasons": list(view.reasons),
                "windows": [dict(w) for w in view.windows],
                "rpeaks": None if view.rpeaks is None else list(view.rpeaks),
                "hr_bpm": view.hr_bpm,
                "hrv": None if view.hrv is None else dict(view.hrv),
            }
        )
    return rows


def findings_entry(archive: Archive, rows: Sequence[Mapping[str, Any]]) -> None:
    whole(archive, FINDINGS_TABLE, FINDING_COLUMNS, rows)


def estimates_entry(archive: Archive, rows: Sequence[Mapping[str, Any]]) -> None:
    whole(archive, ESTIMATES_FILE, ESTIMATE_COLUMNS, rows, label=ESTIMATES_LABEL)
