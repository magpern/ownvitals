"""The ``export_build`` job: the user's complete data as one archive (ADR 0013 sections 1, 2 and
4; #211).

The job runs in the worker, in the requesting user's ``UserContext`` as the job issuer built it
from the claimed row (ADR 0002, ADR 0005 section 4). Its only input is the export's id. Two kinds
of read:

- **owned tables** in the job's fenced sessions, under forced RLS: ``export_read`` (the
  allowlisted columns of every ``GENERIC`` table), ``export_ecg`` (the recordings and their
  signal), and the one read function ``views.findings_read`` for the findings (consumer
  ``export``: every finding with its overlay state) and the up-to-date estimates;
- **account data** only through ``ownvitals.auth.exports.export_account``, which reads by the
  context's user alone. The builder never names an account table (guard 8).

The archive streams into ``export_chunks`` as it is written (``export_archive``), each insert in a
fenced transaction, so a worker that lost its lease writes nothing more. A new attempt first
deletes the chunks of the one before. Over ``export_max_bytes`` the build stops, deletes its
chunks and marks the export ``failed`` with ``export_too_large`` and the largest part. On success
the row becomes ``ready`` with its size, SHA-256 and expiry (``export_ttl_hours``) in one fenced
transaction, and the audit events ``export_ready`` or ``export_failed`` follow.

**Visibility follows the screen** (section 2): estimates and experimental findings are read only
behind the owner-only switch (the read function decides); without it they are absent and the
manifest lists them as withheld with ``experimental_tier_not_enabled``. Progress events carry
counts of files only (T47).
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals import __version__
from ownvitals.auth.exports import (
    ACCOUNT_COLUMNS,
    AccountExport,
    Failed,
    Ready,
    export_account,
    record_event,
)
from ownvitals.config import Settings
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun
from ownvitals.reports.export_allowlist import (
    ESTIMATES_FILE,
    ESTIMATES_LABEL,
    GENERIC,
    WITHHELD_REASON,
    WITHHELD_WITHOUT_TIER,
    TableSpec,
)
from ownvitals.reports.export_archive import Archive, TooLarge
from ownvitals.reports.export_ecg import read_recordings
from ownvitals.reports.export_files import (
    Page,
    estimate_rows,
    estimates_entry,
    finding_rows,
    findings_entry,
    paged,
    recordings,
    whole,
)
from ownvitals.reports.export_format import json_text
from ownvitals.reports.export_read import read_page
from ownvitals.views.findings_read import read_ecg_estimates, read_findings

KIND: Final = "export_build"
FORMAT: Final = "ownvitals-export/1"
# The account's files, the generic tables, the recordings (three entries), findings, estimates.
_STEPS: Final = 2 + len(GENERIC) + 3 + 1 + 1

_ROW = text(
    "SELECT state FROM exports WHERE id = :id AND owner_id = :owner AND kind = 'data' FOR UPDATE"
)
CLEAR_CHUNKS = text("DELETE FROM export_chunks WHERE export_id = :id AND owner_id = :owner")
INSERT_CHUNK = text(
    "INSERT INTO export_chunks (owner_id, export_id, seq, bytes) VALUES (:owner, :id, :seq, :b)"
)
MARK_READY = text(
    "UPDATE exports SET state = 'ready', ready_at = now(), "
    "expires_at = now() + make_interval(hours => :ttl), size = :size, sha256 = :sha "
    "WHERE id = :id AND owner_id = :owner AND state = 'building'"
)
MARK_FAILED = text(
    "UPDATE exports SET state = 'failed', failure = :failure, largest = :largest "
    "WHERE id = :id AND owner_id = :owner AND state = 'building'"
)


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    export_id: UUID


class _Build:
    """One attempt's archive and chunk counter (``export_files.ExportJob``)."""

    def __init__(self, job: JobRun, export_id: UUID, settings: Settings) -> None:
        self.job = job
        self.export_id = export_id
        self.owner = job.ctx.user_id
        self.archive = Archive(max_bytes=settings.export_max_bytes)
        self.seq = 0

    async def _store(self, db: AsyncSession, *, final: bool = False) -> None:
        for chunk in self.archive.take(final=final):
            params = {"owner": self.owner, "id": self.export_id, "seq": self.seq, "b": chunk}
            await db.execute(INSERT_CHUNK, params)
            self.seq += 1

    async def page(self, read: Page, after: UUID | None) -> Sequence[Mapping[str, Any]]:
        async with self.job.fenced() as fs:
            await self._store(fs.db)
            return await read(fs.db, after)

    async def flush(self, *, final: bool = False) -> None:
        async with self.job.fenced() as fs:
            await self._store(fs.db, final=final)


def _generic(owner: UUID, spec: TableSpec, experimental: bool) -> Page:
    async def read(db: AsyncSession, after: UUID | None) -> Sequence[Mapping[str, Any]]:
        return await read_page(db, owner, spec, after, experimental=experimental)

    return read


def _ecg(owner: UUID) -> Page:
    async def read(db: AsyncSession, after: UUID | None) -> Sequence[Mapping[str, Any]]:
        return await read_recordings(db, owner, after)

    return read


def _account(build: _Build, account: AccountExport) -> None:
    user = account.user.model_dump()
    whole(build.archive, "account", ACCOUNT_COLUMNS["users"], [user])
    whole(build.archive, "auth_events", ACCOUNT_COLUMNS["auth_events"], list(account.events))


def manifest(build: _Build, *, generated_at: datetime, experimental: bool) -> dict[str, Any]:
    files = [
        {
            "path": f.path,
            "rows": f.rows,
            "sha256": f.sha256,
            **({"label": f.label} if f.label else {}),
        }
        for f in build.archive.files
    ]
    withheld = (
        []
        if experimental
        else [{"item": item, "reason": WITHHELD_REASON} for item in WITHHELD_WITHOUT_TIER]
    )
    return {
        "format": FORMAT,
        "app_version": __version__,
        "generated_at": generated_at,
        "account_id": build.owner,
        "files": files,
        "withheld": withheld,
    }


async def build_archive(job: JobRun, export_id: UUID, settings: Settings) -> _Build:
    """Every file of the archive, then the manifest, stored as chunks. Raises ``TooLarge``."""
    build = _Build(job, export_id, settings)
    generated_at = datetime.now(UTC)
    account = await export_account(job.factory, job.ctx)
    if account is None:
        raise JobFailure("account_disabled")
    _account(build, account)
    # The read function decides the experimental tier once (the owner-only switch, D9); the
    # experimental scores, findings and estimates all follow that one answer.
    read = await read_findings(job.factory, job.ctx, settings, consumer="export")
    findings_entry(build.archive, finding_rows(read.findings))
    done = 3
    await build.flush()
    await job.progress(done, _STEPS)
    for spec in GENERIC:
        await paged(build, spec, _generic(build.owner, spec, read.experimental))
        done += 1
        await job.progress(done, _STEPS)
    recording_ids = await recordings(build, _ecg(build.owner))
    done += 3
    await job.progress(done, _STEPS)
    if read.experimental:
        estimates = await read_ecg_estimates(job.factory, job.ctx, settings, recording_ids)
        estimates_entry(build.archive, estimate_rows(estimates))
    await build.flush()
    await job.progress(_STEPS, _STEPS)
    build.archive.begin("manifest.json", "manifest")
    shown = manifest(build, generated_at=generated_at, experimental=read.experimental)
    build.archive.write(json_text(shown), rows=1)
    build.archive.end()
    build.archive.close()
    await build.flush(final=True)
    return build


async def _start(job: JobRun, export_id: UUID) -> bool:
    """Whether the export is still building; a retried attempt starts from no chunk."""
    owner = {"id": export_id, "owner": job.ctx.user_id}
    async with job.fenced() as fs:
        state = (await fs.db.execute(_ROW, owner)).scalar_one_or_none()
        if state != "building":
            return False
        await fs.db.execute(CLEAR_CHUNKS, owner)
    return True


async def _fail(job: JobRun, export_id: UUID, largest: str) -> None:
    owner = {"id": export_id, "owner": job.ctx.user_id}
    async with job.fenced() as fs:
        await fs.db.execute(CLEAR_CHUNKS, owner)
        params = {**owner, "failure": "export_too_large", "largest": largest}
        await fs.db.execute(MARK_FAILED, params)
    await record_event(
        job.factory,
        job.ctx,
        "export_failed",
        Failed(export_id=export_id, reason="export_too_large"),
    )


async def run_export(job: JobRun, settings: Settings | None = None) -> Complete:
    parsed = job.input
    if not isinstance(parsed, ExportInput):
        raise JobFailure("invalid_input")
    settings = settings if settings is not None else Settings()
    export_id = parsed.export_id
    if not await _start(job, export_id):
        return Complete()
    try:
        build = await build_archive(job, export_id, settings)
    except TooLarge as too_large:
        too_large.archive.discard()
        await _fail(job, export_id, too_large.largest)
        return Complete()
    owner = {"id": export_id, "owner": job.ctx.user_id}
    ttl = settings.export_ttl_hours
    async with job.fenced() as fs:
        params = {**owner, "ttl": ttl, "size": build.archive.size, "sha": build.archive.sha256}
        await fs.db.execute(MARK_READY, params)
    await record_event(
        job.factory, job.ctx, "export_ready", Ready(export_id=export_id, size=build.archive.size)
    )
    return Complete()


def export_kind(settings: Settings | None = None) -> JobKind:
    async def run(job: JobRun) -> Complete:
        return await run_export(job, settings)

    return JobKind(KIND, ExportInput, run)


def register_export_kind(registry: KindRegistry, settings: Settings | None = None) -> None:
    if registry.get(KIND) is None:
        registry.register(export_kind(settings))


__all__ = [
    "ESTIMATES_FILE",
    "ESTIMATES_LABEL",
    "FORMAT",
    "KIND",
    "ExportInput",
    "build_archive",
    "export_kind",
    "register_export_kind",
    "run_export",
]
