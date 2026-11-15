"""``_recordings_in`` follows ECG pages until the list ends or the period is passed.

Synthetic recordings only; the page reader is faked, no database.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from ownvitals.ai import tools_vitals
from ownvitals.views.ecg_read import LIST_MAX, Recording

SYNTHETIC = True
TODAY = datetime(2026, 6, 30, 12, tzinfo=UTC)


def _recording(at: datetime) -> Recording:
    return Recording(
        id=uuid4(),
        recorded_at=at,
        source_tz="UTC",
        model_id=None,
        afib=0,
        afib_details=None,
        heart_rate=60,
        sampling_hz=None,
        wearposition=None,
        intervals=(),
        has_signal=False,
    )


def _fake_reader(recordings: list[Recording], offsets: list[int]) -> Any:
    async def list_recordings(
        _factory: Any, _ctx: Any, *, limit: int, offset: int
    ) -> tuple[list[Recording], bool]:
        offsets.append(offset)
        page = recordings[offset : offset + limit]
        return page, offset + limit < len(recordings)

    return list_recordings


async def test_every_page_is_read_until_the_list_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    total = 2 * LIST_MAX + 50  # three pages, newest first, one per hour
    recordings = [_recording(TODAY - timedelta(hours=i)) for i in range(total)]
    offsets: list[int] = []
    monkeypatch.setattr(tools_vitals, "list_recordings", _fake_reader(recordings, offsets))
    first = date(2026, 1, 1)
    found = await tools_vitals._recordings_in(None, None, first, TODAY.date())  # type: ignore[arg-type]
    assert offsets == [0, LIST_MAX, 2 * LIST_MAX]
    assert [r.id for _, r in found] == [r.id for r in recordings]


async def test_paging_stops_once_a_page_is_past_the_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two pages inside the period, then a page entirely older than it: the third is the last read.
    recordings = [_recording(TODAY - timedelta(minutes=i)) for i in range(LIST_MAX)]
    recordings += [_recording(TODAY - timedelta(days=5, minutes=i)) for i in range(LIST_MAX)]
    recordings += [_recording(TODAY - timedelta(days=9, minutes=i)) for i in range(LIST_MAX)]
    recordings += [_recording(TODAY - timedelta(days=20, minutes=i)) for i in range(LIST_MAX)]
    offsets: list[int] = []
    monkeypatch.setattr(tools_vitals, "list_recordings", _fake_reader(recordings, offsets))
    first = TODAY.date() - timedelta(days=7)
    found = await tools_vitals._recordings_in(None, None, first, TODAY.date())  # type: ignore[arg-type]
    assert offsets == [0, LIST_MAX, 2 * LIST_MAX]  # the page at 9 days ends the scan
    assert len(found) == 2 * LIST_MAX


async def test_the_scan_keeps_a_one_day_zone_margin_before_the_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that ends within one day before the period's UTC start may still hold a recording
    whose local day is in the period (a zone ahead of UTC), so the next page is read."""
    first = TODAY.date() - timedelta(days=7)
    floor = datetime(first.year, first.month, first.day, tzinfo=UTC) - timedelta(days=1)
    inside = [_recording(floor + timedelta(minutes=LIST_MAX - i)) for i in range(LIST_MAX)]
    older = [_recording(floor - timedelta(hours=i + 1)) for i in range(5)]
    offsets: list[int] = []
    monkeypatch.setattr(tools_vitals, "list_recordings", _fake_reader(inside + older, offsets))
    await tools_vitals._recordings_in(None, None, first, TODAY.date())  # type: ignore[arg-type]
    assert offsets == [0, LIST_MAX]  # the page ending at the margin is not the last read


async def test_the_scan_stops_once_a_page_is_past_the_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TODAY.date() - timedelta(days=7)
    floor = datetime(first.year, first.month, first.day, tzinfo=UTC) - timedelta(days=1)
    past = [_recording(floor - timedelta(minutes=1 + i)) for i in range(LIST_MAX)]
    offsets: list[int] = []
    monkeypatch.setattr(tools_vitals, "list_recordings", _fake_reader(past + past, offsets))
    await tools_vitals._recordings_in(None, None, first, TODAY.date())  # type: ignore[arg-type]
    assert offsets == [0]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "not_reported"),
        (0, "negative"),
        (1, "positive"),
        (2, "inconclusive"),
        (3, "undocumented"),
        (-1, "undocumented"),
    ],
)
def test_classification_maps_every_documented_code(code: int | None, expected: str) -> None:
    assert tools_vitals.classification(code) == expected
