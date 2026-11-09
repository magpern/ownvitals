"""The ``doctor_pdf_build`` job: the doctor PDF built in the worker (ADR 0013 sections 4 and 6;
issue 210).

It runs in the requesting user's ``UserContext`` as the job issuer built it (ADR 0002). Its input
is the user's choices (period, metrics, ECG recording ids) and the id of the ``exports`` row of
kind ``doctor_pdf``; the table, the chunks, the single use and the expiry are those of the data
export (``export_store``). The reads are in ``doctor_read``; the page content is ``doctor_model``
and ``doctor_layout`` (pure); the bytes come from ``doctor_pdf``.

The PDF is built in memory (a few MiB at most: 10 strips and at most 12 months), stored in
``export_chunks`` in fenced transactions, and the row becomes ``ready`` with its size, SHA-256
and expiry in one fenced transaction. A build error ends the job failed; the user's next tidy
marks the row ``build_failed`` (``export_store.tidy``).
"""

import hashlib
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text

from ownvitals.auth.exports import Ready, record_event
from ownvitals.config import Settings
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun
from ownvitals.reports.doctor_input import DoctorInput
from ownvitals.reports.doctor_model import EcgNote, Report, build_section
from ownvitals.reports.doctor_pdf import build_doctor_pdf, strip_pages
from ownvitals.reports.doctor_read import (
    read_birth_year,
    read_daily,
    read_device_findings,
    read_strips,
)
from ownvitals.reports.ecg_layout import Page
from ownvitals.reports.export import CLEAR_CHUNKS, INSERT_CHUNK, MARK_READY

KIND: Final = "doctor_pdf_build"
CHUNK_BYTES: Final = 4 << 20

_ROW = text(
    "SELECT state FROM exports WHERE id = :id AND owner_id = :owner AND kind = 'doctor_pdf' "
    "FOR UPDATE"
)


async def _start(job: JobRun, export_id: UUID) -> bool:
    """Whether the export is still building; a retried attempt starts from no chunk."""
    owner = {"id": export_id, "owner": job.ctx.user_id}
    async with job.fenced() as fs:
        state = (await fs.db.execute(_ROW, owner)).scalar_one_or_none()
        if state != "building":
            return False
        await fs.db.execute(CLEAR_CHUNKS, owner)
    return True


async def build_report(job: JobRun, parsed: DoctorInput, settings: Settings) -> bytes:
    """The PDF's bytes for ``parsed``'s choices, read as the job's user."""
    async with job.fenced() as fs:
        daily = await read_daily(fs.db, job.ctx.user_id, parsed.metrics, parsed.start, parsed.end)
    sections = tuple(
        build_section(key, daily.get(key, []), parsed.start, parsed.end) for key in parsed.metrics
    )
    await job.progress(1, 4)
    birth_year = await read_birth_year(job, settings)
    findings = await read_device_findings(job, settings, parsed.start, parsed.end)
    await job.progress(2, 4)
    facts, notes = await read_strips(job, parsed.recordings)
    strips: list[tuple[Page, ...]] = []
    for one in facts:
        pages = strip_pages(one)
        if pages is None:
            notes.append(EcgNote("a recording", "too long to print at true scale"))
        else:
            strips.append(pages)
    await job.progress(3, 4)
    report = Report(
        start=parsed.start,
        end=parsed.end,
        generated=datetime.now(UTC).date(),
        birth_year=birth_year,
        sections=sections,
        findings=findings,
        ecg_notes=tuple(notes),
    )
    return build_doctor_pdf(report, strips)


async def run_doctor(job: JobRun, settings: Settings | None = None) -> Complete:
    parsed = job.input
    if not isinstance(parsed, DoctorInput):
        raise JobFailure("invalid_input")
    settings = settings if settings is not None else Settings()
    export_id = parsed.export_id
    if not await _start(job, export_id):
        return Complete()
    pdf = await build_report(job, parsed, settings)
    owner = {"id": export_id, "owner": job.ctx.user_id}
    for seq, start in enumerate(range(0, len(pdf), CHUNK_BYTES)):
        async with job.fenced() as fs:
            chunk = {**owner, "seq": seq, "b": pdf[start : start + CHUNK_BYTES]}
            await fs.db.execute(INSERT_CHUNK, chunk)
    async with job.fenced() as fs:
        ready = {
            **owner,
            "ttl": settings.export_ttl_hours,
            "size": len(pdf),
            "sha": hashlib.sha256(pdf).digest(),
        }
        await fs.db.execute(MARK_READY, ready)
    detail = Ready(export_id=export_id, size=len(pdf), export_kind="doctor_pdf")
    await record_event(job.factory, job.ctx, "export_ready", detail)
    await job.progress(4, 4)
    return Complete()


def doctor_kind(settings: Settings | None = None) -> JobKind:
    async def run(job: JobRun) -> Complete:
        return await run_doctor(job, settings)

    return JobKind(KIND, DoctorInput, run)


def register_doctor_kind(registry: KindRegistry, settings: Settings | None = None) -> None:
    if registry.get(KIND) is None:
        registry.register(doctor_kind(settings))


__all__ = ["KIND", "build_report", "doctor_kind", "register_doctor_kind", "run_doctor"]
