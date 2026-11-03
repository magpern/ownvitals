"""The pure part of the daily pass (#186): which local day a reading lands on, which readings a
value came from, the mean, determinism, and the fingerprint. No database.

Synthetic readings only (``synthetic_`` values chosen for the case; no real measurement).
"""

import random
import statistics
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ownvitals.analysis.quality import METRICS
from ownvitals.derived import daily as module
from ownvitals.derived.daily import ObservationRow, compute, fingerprint
from ownvitals.derived.tables import METRIC_KEY_PATTERN

STO = "Europe/Stockholm"
NYC = "America/New_York"


def obs(
    grpid: int,
    when: str,
    value: str,
    *,
    tz: str = STO,
    key: str = "weight",
    flags: tuple[str, ...] = (),
) -> ObservationRow:
    return ObservationRow(
        metric_key=key,
        source_grpid=grpid,
        source_type=1,
        source_position=None,
        ts=datetime.fromisoformat(when).replace(tzinfo=UTC),
        source_tz=tz,
        value=Decimal(value),
        quality_flags=flags,
    )


def by_day(rows: tuple[module.DailyRow, ...]) -> dict[date, module.DailyRow]:
    return {r.day: r for r in rows}


def test_the_keys_are_the_analysed_metrics_and_match_the_documented_pattern() -> None:
    import re

    assert tuple(sorted(METRICS)) == module.KEYS
    assert all(re.fullmatch(METRIC_KEY_PATTERN, k) for k in module.KEYS)
    assert module.ALGO_VERSION == "daily/1"


def test_readings_around_local_midnight_land_on_the_local_day() -> None:
    # 2026-10-20 21:59:59 UTC is 23:59:59 in Stockholm (CEST); one second later is the 21st.
    rows = compute(
        [obs(1, "2026-10-20T21:59:59", "80.0"), obs(2, "2026-10-20T22:00:00", "81.0")]
    ).rows
    assert {d: r.value for d, r in by_day(rows).items()} == {
        date(2026, 10, 20): Decimal("80.0"),
        date(2026, 10, 21): Decimal("81.0"),
    }


def test_spring_forward_and_fall_back_keep_each_reading_on_its_day() -> None:
    spring = compute(
        [
            obs(1, "2026-03-28T23:30:00", "80.0"),  # 00:30 CET on the 29th
            obs(2, "2026-03-29T00:59:00", "80.2"),  # 01:59 CET, last minute before the jump
            obs(3, "2026-03-29T01:00:00", "80.4"),  # 03:00 CEST: the hour 02:xx does not exist
            obs(4, "2026-03-29T01:59:00", "80.6"),  # 03:59 CEST: not yet morning
            obs(5, "2026-03-29T02:00:00", "80.8"),  # 04:00 CEST: the first morning reading
        ]
    ).rows
    [day] = spring
    assert (day.day, day.method, day.value, day.n) == (
        date(2026, 3, 29), "morning", Decimal("80.8"), 5,
    )  # fmt: skip
    assert day.source_groups == (5,)
    fall = compute(
        [
            obs(1, "2026-10-24T22:30:00", "70.0"),  # 00:30 CEST on the 25th
            obs(2, "2026-10-25T00:30:00", "71.0"),  # 02:30 CEST
            obs(3, "2026-10-25T01:30:00", "72.0"),  # 02:30 CET, the repeated hour
            obs(4, "2026-10-25T02:59:00", "73.0"),  # 03:59 CET: not morning
            obs(5, "2026-10-25T03:00:00", "74.0"),  # 04:00 CET: morning
            obs(6, "2026-10-25T23:00:00", "75.0"),  # 00:00 on the 26th
        ]
    ).rows
    assert [(r.day, r.method, r.value, r.n) for r in fall] == [
        (date(2026, 10, 25), "morning", Decimal("74.0"), 5),
        (date(2026, 10, 26), "median", Decimal("75.0"), 1),
    ]


def test_a_trip_assigns_each_reading_to_the_day_of_its_own_zone() -> None:
    rows = compute(
        [
            obs(1, "2026-06-10T22:30:00", "80.0", tz=STO),  # 00:30 on the 11th in Stockholm
            obs(2, "2026-06-10T22:45:00", "81.0", tz=NYC),  # 18:45 on the 10th in New York
            obs(3, "2026-06-11T06:00:00", "82.0", tz=STO),  # 08:00 Stockholm, the 11th
            obs(4, "2026-06-11T12:00:00", "83.0", tz=NYC),  # 08:00 New York, the 11th
        ]
    ).rows
    days = by_day(rows)
    assert sorted(days) == [date(2026, 6, 10), date(2026, 6, 11)]
    assert days[date(2026, 6, 10)].source_groups == (2,)
    eleventh = days[date(2026, 6, 11)]
    assert (eleventh.method, eleventh.value, eleventh.n) == ("morning", Decimal("82.0"), 3)
    assert eleventh.source_groups == (3,)  # the earliest instant that is morning where taken


def test_the_median_uses_every_reading_of_the_day_and_records_them_all() -> None:
    [day] = compute(
        [
            obs(7, "2026-10-20T14:00:00", "80.0"),
            obs(3, "2026-10-20T16:00:00", "82.0"),
            obs(9, "2026-10-20T18:00:00", "81.0"),
        ]
    ).rows
    assert (day.method, day.value, day.morning, day.n) == ("median", Decimal("81.0"), None, 3)
    assert day.source_groups == (3, 7, 9)  # ascending, every reading the median used
    assert (day.minimum, day.maximum, day.mad) == (Decimal("80.0"), Decimal("82.0"), Decimal(1))
    assert day.mean == Decimal("81.0")


def test_a_morning_value_is_the_morning_reading_and_the_mean_covers_the_whole_day() -> None:
    [day] = compute(
        [
            obs(1, "2026-10-20T06:30:00", "80.0"),  # 08:30 local
            obs(2, "2026-10-20T14:00:00", "81.0"),
            obs(3, "2026-10-20T16:00:00", "82.0"),
        ]
    ).rows
    assert (day.method, day.value, day.morning, day.source_groups) == (
        "morning", Decimal("80.0"), Decimal("80.0"), (1,),
    )  # fmt: skip
    assert day.mean == Decimal("81.0")
    assert day.n == 3


def test_a_mean_without_a_finite_form_is_rounded_and_stays_between_min_and_max() -> None:
    [day] = compute(
        [
            obs(1, "2026-10-20T14:00:00", "80.0"),
            obs(2, "2026-10-20T15:00:00", "80.0"),
            obs(3, "2026-10-20T16:00:00", "81.0"),
        ]
    ).rows
    assert day.minimum <= day.mean <= day.maximum
    assert str(day.mean).startswith("80.33333333333333")


def test_excluded_readings_and_other_metrics_are_not_used_but_jumps_are() -> None:
    rows = compute(
        [
            obs(1, "2026-10-20T14:00:00", "80.0"),
            obs(2, "2026-10-20T15:00:00", "99.0", flags=("unconfirmed",)),
            obs(3, "2026-10-20T16:00:00", "9.0", flags=("implausible",)),
            obs(4, "2026-10-20T17:00:00", "90.0", flags=("duplicate",)),
            obs(5, "2026-10-20T18:00:00", "84.0", flags=("jump",)),
            obs(6, "2026-10-20T18:00:00", "70.0", key="meastype_4242"),  # not analysed
            obs(7, "2026-10-20T18:00:00", "170.0", key="segmental_muscle_mass:left_arm"),
        ]
    ).rows
    [day] = rows
    assert day.metric_key == "weight"
    assert day.source_groups == (1, 5)
    assert day.n == 2
    assert day.value == Decimal("82.0")


def test_a_day_whose_readings_are_all_excluded_has_no_row() -> None:
    assert compute([obs(1, "2026-10-20T14:00:00", "80.0", flags=("unconfirmed",))]).rows == ()
    assert compute([]).rows == ()


def test_unusable_observations_are_counted_not_raised() -> None:
    computed = compute(
        [
            obs(1, "2026-10-20T14:00:00", "80.0"),
            obs(2, "2026-10-20T15:00:00", "80.0", tz="Not/AZone"),
            obs(3, "2026-10-20T16:00:00", "1" + "0" * 40),  # more than 30 digits
        ]
    )
    assert computed.skipped == 2
    assert [r.source_groups for r in computed.rows] == [(1,)]


def test_a_repeated_group_is_used_once_and_counted() -> None:
    first = obs(1, "2026-10-20T14:00:00", "80.0")
    # A second copy of the same group under another source type, an hour later.
    again = ObservationRow(
        "weight", 1, 2, None, first.ts + timedelta(hours=1), STO, Decimal("99.0"), ()
    )
    computed = compute([first, again])
    assert computed.skipped == 1
    assert [(r.n, r.value) for r in computed.rows] == [(1, Decimal("80.0"))]


def test_the_result_does_not_depend_on_the_order_of_the_input() -> None:
    rows = [
        obs(i, f"2026-10-{20 + i % 4}T{6 + i % 12:02d}:{i % 60:02d}:00", f"{80 + i % 5}.{i % 10}")
        for i in range(1, 40)
    ] + [obs(100 + i, f"2026-10-20T0{i}:00:00", f"{60 + i}", key="spo2") for i in range(1, 5)]
    expected = compute(rows)
    shuffler = random.Random(186)  # noqa: S311 - a seeded test generator
    for _ in range(10):
        shuffler.shuffle(rows)
        assert compute(rows) == expected
        assert fingerprint(rows) == fingerprint(sorted(rows, key=lambda r: r.source_grpid))


def test_source_groups_are_the_readings_the_value_used() -> None:
    """Property test (seeded): an oracle written from the rules, not from ``daily_values``."""
    rng = random.Random(1860)  # noqa: S311 - a seeded test generator, not a secret
    zones = [STO, NYC, "Asia/Tokyo", "Australia/Lord_Howe", "UTC"]
    flags = [(), (), (), ("unconfirmed",), ("implausible",), ("jump",)]
    base = datetime(2026, 3, 27, tzinfo=UTC)  # spans the EU spring-forward
    for _ in range(150):
        rows = [
            obs(
                grpid,
                (base + timedelta(minutes=rng.randrange(0, 5 * 24 * 60))).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
                str(Decimal(rng.randrange(600, 1000)) / 10),
                tz=rng.choice(zones),
                key=rng.choice(["weight", "heart_pulse"]),
                flags=rng.choice(flags),
            )
            for grpid in range(1, rng.randrange(2, 30))
        ]
        used: dict[tuple[str, date], list[tuple[datetime, int, Decimal, int]]] = {}
        for r in rows:
            if {"unconfirmed", "implausible", "duplicate"} & set(r.quality_flags):
                continue
            local = r.ts.astimezone(ZoneInfo(r.source_tz))
            used.setdefault((r.metric_key, local.date()), []).append(
                (r.ts, r.source_grpid, r.value, local.hour)
            )
        result = {r.key: r for r in compute(rows).rows}
        assert set(result) == set(used)
        for key, readings in used.items():
            row = result[key]
            readings.sort(key=lambda t: (t[0], t[1]))
            mornings = [t for t in readings if 4 <= t[3] < 11]
            assert row.n == len(readings)
            if mornings:
                assert row.method == "morning"
                assert row.source_groups == (mornings[0][1],)
                assert row.value == row.morning == mornings[0][2]
            else:
                assert row.method == "median"
                assert row.morning is None
                assert row.source_groups == tuple(sorted(t[1] for t in readings))
                assert row.value == statistics.median(t[2] for t in readings)
            assert row.minimum == min(t[2] for t in readings)
            assert row.maximum == max(t[2] for t in readings)
            assert row.minimum <= row.mean <= row.maximum


def test_the_fingerprint_follows_the_content_and_not_the_order() -> None:
    a, b = obs(1, "2026-10-20T14:00:00", "80.0"), obs(2, "2026-10-21T14:00:00", "81.0")
    assert fingerprint([a, b]) == fingerprint([b, a])
    assert len(fingerprint([a])) == 32
    assert fingerprint([a, b]) != fingerprint([a])
    assert fingerprint([a]) != fingerprint([obs(1, "2026-10-20T14:00:00", "80.1")])
    assert fingerprint([a]) != fingerprint([obs(1, "2026-10-20T14:00:01", "80.0")])
    assert fingerprint([a]) != fingerprint([obs(1, "2026-10-20T14:00:00", "80.0", tz=NYC)])
    assert fingerprint([a]) != fingerprint([obs(1, "2026-10-20T14:00:00", "80.0", flags=("jump",))])
    assert fingerprint([a]) != fingerprint([obs(2, "2026-10-20T14:00:00", "80.0")])
    # An observation of a metric this pass does not read does not move it.
    assert fingerprint([a]) == fingerprint(
        [a, obs(9, "2026-10-20T14:00:00", "1", key="meastype_9")]
    )


def test_a_changed_algorithm_version_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = obs(1, "2026-10-20T14:00:00", "80.0")
    before = fingerprint([a])
    monkeypatch.setattr(module, "ALGO_VERSION", "daily/2")
    assert fingerprint([a]) != before
