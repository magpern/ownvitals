"""The AI tools' period ends on the user's local day, not on ``now``'s UTC date (issue #443).

Synthetic data only; the reader and the ECG page reader are faked, no database.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ownvitals.ai import local_day_read, tools, tools_vitals
from ownvitals.ai.local_day import local_today
from ownvitals.ai.tools_model import DailyValuesArgs, FindingsArgs, MetricSummaryArgs
from ownvitals.ai.tools_vitals import ecg_summary, sleep_summary
from ownvitals.ai.tools_vitals_model import EcgSummaryArgs, SleepSummaryArgs
from ownvitals.views.ecg_read import Recording
from ownvitals.views.read_records import DailyRecord
from ownvitals.views.reads import Reader

SYNTHETIC = True
NONE: Any = None  # factory, context and settings are unused by the faked reader
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

    async def home_zone(self, now: datetime) -> str | None:
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
    await sleep_summary(NONE, NONE, NONE, SleepSummaryArgs(period=7), NOW)
    assert reader.asked == [(date(2026, 6, 25), date(2026, 7, 1))]


async def test_no_zone_keeps_the_utc_day_in_the_sleep_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _FakeReader(None)
    monkeypatch.setattr(tools_vitals, "reading", _fake_reading(reader))
    await sleep_summary(NONE, NONE, NONE, SleepSummaryArgs(period=7), NOW)
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
    monkeypatch.setattr(local_day_read, "reading", _fake_reading(_FakeReader("Australia/Sydney")))
    out = await ecg_summary(NONE, NONE, NONE, EcgSummaryArgs(period=30), NOW)
    assert out.recordings == 1
    assert out.items[0].day == date(2026, 7, 1)

    monkeypatch.setattr(local_day_read, "reading", _fake_reading(_FakeReader(None)))
    old = await ecg_summary(NONE, NONE, NONE, EcgSummaryArgs(period=30), NOW)
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
    assert await _reader(db).home_zone(NOW) == "Europe/Stockholm"
    assert db.queries == 1


async def test_home_zone_else_the_newest_reading_zone_else_none() -> None:
    assert (
        await _reader(_Db(_Profile("reading_tz", None), "Asia/Tokyo")).home_zone(NOW)
        == "Asia/Tokyo"
    )
    assert await _reader(_Db(None, "Asia/Tokyo")).home_zone(NOW) == "Asia/Tokyo"
    assert await _reader(_Db(None, None)).home_zone(NOW) is None


# The tools in ``ai/tools.py``: each period must end on the Sydney day (1 July), not the UTC one.
class _Stop(Exception):
    pass


class _ToolReader(_FakeReader):
    def __init__(self, zone: str | None, records: list[DailyRecord]) -> None:
        super().__init__(zone)
        self.records = records

    async def status_facts(self, now: datetime) -> None:
        return None

    async def daily(self, keys: Any) -> dict[str, list[DailyRecord]]:
        return {k: self.records for k in keys}

    async def measures_revision(self) -> int:
        return 1


def _daily(day: date) -> DailyRecord:
    one = Decimal("70")
    return DailyRecord("weight", day, one, "morning", 1, one, one, Decimal(0), "v1", 1)


def _patch_reading(monkeypatch: pytest.MonkeyPatch, reader: _FakeReader) -> None:
    monkeypatch.setattr(tools, "reading", _fake_reading(reader))
    monkeypatch.setattr(local_day_read, "reading", _fake_reading(reader))
    monkeypatch.setattr(tools, "settle", lambda *_a, **_k: None)


async def test_list_metrics_tiles_use_the_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reading(monkeypatch, _ToolReader("Australia/Sydney", [_daily(date(2026, 6, 30))]))
    seen: list[date] = []

    def tile(*_a: Any, today: date, **_k: Any) -> Any:
        seen.append(today)
        raise _Stop

    monkeypatch.setattr(tools.assemble, "tile", tile)
    with pytest.raises(_Stop):
        await tools._list_metrics(NONE, NONE, NONE, None, NOW)
    assert seen == [date(2026, 7, 1)]


async def test_metric_summary_anchors_on_the_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reading(monkeypatch, _ToolReader("Australia/Sydney", []))
    seen: list[date] = []

    def detail(*_a: Any, anchor: date, **_k: Any) -> Any:
        seen.append(anchor)
        raise _Stop

    monkeypatch.setattr(tools.assemble_detail, "detail", detail)
    with pytest.raises(_Stop):
        await tools._metric_summary(
            NONE, NONE, NONE, MetricSummaryArgs(metric="weight", period=30), NOW
        )
    assert seen == [date(2026, 7, 1)]


async def test_daily_values_window_ends_on_the_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """A point on 1 June is inside a 30-day window ending 30 June (UTC) but not 1 July."""
    records = [_daily(date(2026, 6, 1)), _daily(date(2026, 6, 20))]
    _patch_reading(monkeypatch, _ToolReader("Australia/Sydney", records))
    out = await tools._daily_values(
        NONE, NONE, NONE, DailyValuesArgs(metric="weight", period=30), NOW
    )
    assert [p.day for p in out.points] == [date(2026, 6, 20)]
    _patch_reading(monkeypatch, _ToolReader(None, records))
    old = await tools._daily_values(
        NONE, NONE, NONE, DailyValuesArgs(metric="weight", period=30), NOW
    )
    assert [p.day for p in old.points] == [date(2026, 6, 1), date(2026, 6, 20)]


async def test_findings_period_starts_from_the_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    def finding(end: date) -> Any:
        return SimpleNamespace(
            rule_id="R1", tier="a", severity="info", confidence="high", origin="rule",
            status="active", window_start=end, window_end=end, metric_keys=[],
        )  # fmt: skip

    async def read_findings(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(findings=[finding(date(2026, 6, 1)), finding(date(2026, 6, 2))])

    _patch_reading(monkeypatch, _FakeReader("Australia/Sydney"))
    monkeypatch.setattr(tools, "read_findings", read_findings)
    out = await tools._findings(NONE, NONE, NONE, FindingsArgs(status="active", period=30), NOW)
    assert [f.window_end for f in out.findings] == [date(2026, 6, 2)]  # since = 2 June
