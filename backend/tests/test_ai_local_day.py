"""The AI tools' period ends on the user's local day, not on ``now``'s UTC date (issue #443).

Synthetic data only; the reader and the ECG page reader are faked, no database.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from ownvitals.ai import local_day, tools_vitals
from ownvitals.ai.local_day import local_today
from ownvitals.ai.tools_vitals import ecg_summary, sleep_summary
from ownvitals.ai.tools_vitals_model import EcgSummaryArgs, SleepSummaryArgs
from ownvitals.views.ecg_read import Recording
from ownvitals.views.reads import Reader

SYNTHETIC = True
# 23:30 UTC on 30 June is already 1 July in Sydney and still 30 June in New York.
NOW = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)


def test_local_today_follows_the_zone() -> None:
    assert local_today(NOW, "Australia/Sydney") == date(2026, 7, 1)
    assert local_today(NOW, "America/New_York") == date(2026, 6, 30)
    assert local_today(NOW, "UTC") == date(2026, 6, 30)


@pytest.mark.parametrize("zone", [None, "", "Not/AZone", "../etc/passwd"])
def test_local_today_falls_back_to_utc(zone: str | None) -> None:
    assert local_today(NOW, zone) == date(2026, 6, 30)


def test_local_today_refuses_a_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        local_today(datetime(2026, 6, 30, 12), "UTC")


class _FakeReader:
    def __init__(self, zone: str | None) -> None:
        self.zone = zone
        self.asked: list[tuple[date, date]] = []

    async def home_zone(self) -> str | None:
        return self.zone

    async def stream_days(self, keys: Any, first: date, last: date) -> dict[str, dict[date, float]]:
        self.asked.append((first, last))
        return {}


def _fake_reading(reader: _FakeReader) -> Any:
    @asynccontextmanager
    async def reading(_factory: Any, _ctx: Any) -> AsyncIterator[_FakeReader]:
        yield reader

    return reading


async def test_a_zone_ahead_of_utc_keeps_its_last_local_day_in_the_sleep_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _FakeReader("Australia/Sydney")
    monkeypatch.setattr(tools_vitals, "reading", _fake_reading(reader))
    await sleep_summary(None, None, None, SleepSummaryArgs(period=7), NOW)  # type: ignore[arg-type]
    assert reader.asked == [(date(2026, 6, 25), date(2026, 7, 1))]


async def test_no_zone_keeps_the_utc_day_in_the_sleep_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _FakeReader(None)
    monkeypatch.setattr(tools_vitals, "reading", _fake_reading(reader))
    await sleep_summary(None, None, None, SleepSummaryArgs(period=7), NOW)  # type: ignore[arg-type]
    assert reader.asked == [(date(2026, 6, 24), date(2026, 6, 30))]


def _recording(at: datetime, zone: str) -> Recording:
    return Recording(
        id=uuid4(),
        recorded_at=at,
        source_tz=zone,
        model_id=None,
        afib=0,
        afib_details=None,
        heart_rate=60,
        sampling_hz=None,
        wearposition=None,
        intervals=(),
        has_signal=False,
    )


async def test_an_ecg_from_the_last_local_hours_is_in_the_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded 23:10 UTC = 09:10 on 1 July in Sydney: past the UTC date, inside the local day."""
    recording = _recording(datetime(2026, 6, 30, 23, 10, tzinfo=UTC), "Australia/Sydney")

    async def list_recordings(*_a: Any, **_k: Any) -> tuple[list[Recording], bool]:
        return [recording], False

    async def no_estimates(*_a: Any, **_k: Any) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(tools_vitals, "list_recordings", list_recordings)
    monkeypatch.setattr(tools_vitals, "read_ecg_estimates", no_estimates)
    monkeypatch.setattr(local_day, "reading", _fake_reading(_FakeReader("Australia/Sydney")))
    out = await ecg_summary(None, None, None, EcgSummaryArgs(period=30), NOW)  # type: ignore[arg-type]
    assert out.recordings == 1
    assert out.items[0].day == date(2026, 7, 1)

    monkeypatch.setattr(local_day, "reading", _fake_reading(_FakeReader(None)))
    old = await ecg_summary(None, None, None, EcgSummaryArgs(period=30), NOW)  # type: ignore[arg-type]
    assert old.recordings == 0  # the old UTC anchor, for contrast


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    """Answers the profile query, then the newest-reading-zone query."""

    def __init__(self, profile: Any, newest: str | None) -> None:
        self.profile, self.newest, self.queries = profile, newest, 0

    async def execute(self, _q: Any, _p: Any) -> _Result:
        self.queries += 1
        return _Result(self.profile if self.queries == 1 else self.newest)


class _Profile:
    def __init__(self, assignment: str, tz: str | None) -> None:
        self.day_assignment, self.day_tz = assignment, tz


def _reader(db: _Db) -> Reader:
    reader = object.__new__(Reader)
    reader._db, reader._owner = db, uuid4()  # type: ignore[assignment,misc]
    return reader


async def test_home_zone_prefers_the_fixed_profile_zone() -> None:
    db = _Db(_Profile("fixed_tz", "Europe/Stockholm"), "Asia/Tokyo")
    assert await _reader(db).home_zone() == "Europe/Stockholm"
    assert db.queries == 1


async def test_home_zone_else_the_newest_reading_zone_else_none() -> None:
    assert (
        await _reader(_Db(_Profile("reading_tz", None), "Asia/Tokyo")).home_zone() == "Asia/Tokyo"
    )
    assert await _reader(_Db(None, "Asia/Tokyo")).home_zone() == "Asia/Tokyo"
    assert await _reader(_Db(None, None)).home_zone() is None
