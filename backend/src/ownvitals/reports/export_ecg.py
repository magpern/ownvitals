"""The export's reader of the user's ECG recordings (ADR 0013 section 1, ADR 0006 section 11;
#211): Withings' own values and the stored signal, never our derived column.

Like ``views.ecg_read`` it is one of the modules ``tests/test_findings_read_guard.py`` allows to
name the recordings table: it selects exactly ``export_allowlist.SPECIAL_COLUMNS`` of it (the
estimates come only through ``views.findings_read.read_ecg_estimates``, and only for an account
with the experimental tier). It runs in the export job's fenced session, under forced RLS, and
the statement filters ``owner_id`` too.

The signal is decoded into its leads (integers, in microvolts as Withings sends them). A signal
that is missing or does not decode is exported as ``null``; nothing is repaired or guessed, and
the signal is never logged or part of an error.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.codec import decode_signal

PAGE_RECORDINGS: Final = 25
_LOG = logging.getLogger(__name__)

# The columns of ``export_allowlist.SPECIAL_COLUMNS`` for the recordings, in that order.
COLUMNS: Final = (
    "id",
    "connection_id",
    "signalid",
    "recorded_at",
    "source_tz",
    "model_id",
    "afib",
    "heart_rate",
    "sampling_hz",
    "wearposition",
    "reported",
    "afib_details",
    "signal",
)
_PAGE = text(
    "SELECT id, connection_id, signalid, recorded_at, source_tz, model_id, afib, heart_rate, "
    "sampling_hz, wearposition, reported, afib_details, signal FROM public.ecg_recordings "
    "WHERE owner_id = :owner AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
    "ORDER BY id LIMIT :limit"
)


def leads_of(frame: object) -> list[list[int]] | None:
    if frame is None:
        return None
    try:
        return decode_signal(bytes(frame))  # pyright: ignore[reportArgumentType]
    except Exception as exc:  # a frame that does not decode is "no signal"
        _LOG.warning("ecg signal not decodable for export: %s", type(exc).__name__)
        return None


async def read_recordings(
    db: AsyncSession, owner: UUID, after: UUID | None, *, limit: int = PAGE_RECORDINGS
) -> Sequence[Mapping[str, Any]]:
    """The next page of the owner's recordings after ``after``, the signal decoded."""
    rows = (
        (await db.execute(_PAGE, {"owner": owner, "after": after, "limit": limit})).mappings().all()
    )
    return [{**row, "signal": leads_of(row["signal"])} for row in rows]
