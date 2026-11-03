"""Normalisation of the sleep and activity streams, pure part (#229): the key tables, exact
values, the instants, skips, duplicates, order independence and the fingerprint. No database.

Synthetic records only (``synthetic: true``): hand-written values, no real account or night.
"""

import random
import re
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from ownvitals.derived import normalize_streams as ns
from ownvitals.derived.normalize import GroupRef
from ownvitals.derived.tables import METRIC_KEY_PATTERN
from ownvitals.ingest.codec import ACTIVITY_FIELDS
from ownvitals.withings.models import SleepSummaryData, WorkoutData

NOW = datetime(2026, 11, 1, tzinfo=UTC)
WAKE = datetime(2026, 10, 20, 5, 40, tzinfo=UTC)
BED = datetime(2026, 10, 19, 21, 55, tzinfo=UTC)


def night(ref: int | None = 9001, **fields: Any) -> ns.StreamRecord:
    return ns.StreamRecord(ref, WAKE, BED, "Europe/Stockholm", tuple(fields.items()))


def day(on: date, tz: str = "Europe/Stockholm", **fields: Any) -> ns.StreamRecord:
    start = datetime(on.year, on.month, on.day, tzinfo=UTC)
    return ns.StreamRecord(None, None, start, tz, tuple(fields.items()), noon_of=on)


def test_the_key_tables_are_pinned() -> None:
    """Every sleep and activity input of ADR 0012 has a key, and each key is the one below."""
    assert dict(ns.SCORE_INPUTS) == {
        "night_hr": "sleep_hr_average",
        "sleep_ahi": "sleep_apnea_hypopnea_index",
        "readiness_sleep_duration": "sleep_total_sleep_time",
        "readiness_night_hr": "sleep_hr_average",
        "readiness_hrv": "sleep_rmssd",
        "daily_steps": "activity_steps",
        "weekly_active_minutes_moderate": "activity_moderate",
        "weekly_active_minutes_intense": "activity_intense",
    }
    known = {
        **{ns.SLEEP_PREFIX + f: f for f in ns.SLEEP_FIELDS},
        **{ns.ACTIVITY_PREFIX + f: f for f in ns.ACTIVITY_FIELDS},
    }
    assert set(ns.SCORE_INPUTS.values()) <= set(known)


def test_the_field_tables_follow_the_models_and_the_ingest() -> None:
    sleep_numeric = set(SleepSummaryData.model_fields) - {"raw", "night_events"}
    # ``rmssd`` is ADR 0012 §4's HRV, assumed (the public summary shape does not list it).
    assert set(ns.SLEEP_FIELDS) == sleep_numeric | {"rmssd"}
    assert ns.ACTIVITY_FIELDS == ACTIVITY_FIELDS
    assert set(ns.WORKOUT_FIELDS) == set(WorkoutData.model_fields) - {"raw"}
    prefixes = [spec.prefix for spec in (ns.SLEEP, ns.ACTIVITY, ns.WORKOUT)]
    for a in prefixes:  # the ownership marks are disjoint: none starts another
        assert [b for b in prefixes if b.startswith(a)] == [a]
    for spec in (ns.SLEEP, ns.ACTIVITY, ns.WORKOUT):
        assert len(set(spec.known)) == len(spec.known)
        for name in spec.known:
            assert re.fullmatch(METRIC_KEY_PATTERN, spec.prefix + name), name


def test_a_night_becomes_exact_rows_at_its_wake_up() -> None:
    out = ns.normalise_stream(
        [night(total_sleep_time=25_800, hr_average="54.5", apnea_hypopnea_index="3.25")],
        ns.SLEEP,
        now=NOW,
    )
    rows = {row.metric_key: row for row in out.observations}
    assert list(rows) == [
        "sleep_apnea_hypopnea_index",
        "sleep_hr_average",
        "sleep_total_sleep_time",
    ]
    ahi = rows["sleep_apnea_hypopnea_index"]
    assert (ahi.value, ahi.source_value_raw, ahi.source_unit, ahi.conversion) == (
        Decimal("3.25"), 325, -2, "e-2:unknown",
    )  # fmt: skip
    assert rows["sleep_total_sleep_time"].conversion == "e0:unknown"
    for row in rows.values():
        assert (row.ts, row.source_ts, row.source_tz) == (WAKE, BED, "Europe/Stockholm")
        assert (row.source_grpid, row.source_type, row.source_position) == (9001, None, None)
        assert row.quality_flags == ()
    assert out.skipped.total() == 0


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Europe/Stockholm", datetime(2026, 10, 20, 10, 0, tzinfo=UTC)),  # CEST, UTC+2
        ("America/Los_Angeles", datetime(2026, 10, 20, 19, 0, tzinfo=UTC)),  # PDT, UTC-7
        ("Pacific/Kiritimati", datetime(2026, 10, 19, 22, 0, tzinfo=UTC)),  # UTC+14
    ],
)
def test_an_activity_day_is_noon_local_so_its_date_stays(zone: str, expected: datetime) -> None:
    out = ns.normalise_stream([day(date(2026, 10, 20), zone, steps=8_412)], ns.ACTIVITY, now=NOW)
    (row,) = out.observations
    assert (row.metric_key, row.ts, row.source_grpid, row.value) == (
        "activity_steps", expected, None, Decimal(8_412),
    )  # fmt: skip
    assert row.source_ts == datetime(2026, 10, 20, tzinfo=UTC)


def test_unknown_fields_are_kept_for_sleep_and_workouts_but_not_activity() -> None:
    sleep = ns.normalise_stream([night(future_index="7")], ns.SLEEP, now=NOW)
    assert [r.metric_key for r in sleep.observations] == ["sleep_future_index"]
    workout = ns.StreamRecord(77, WAKE, WAKE, "UTC", (("calories", "312.5"), ("new_metric", 4)))
    out = ns.normalise_stream([workout], ns.WORKOUT, now=NOW)
    assert [r.metric_key for r in out.observations] == ["workout_calories", "workout_new_metric"]
    activity = ns.normalise_stream([day(date(2026, 10, 20), new_metric=4)], ns.ACTIVITY, now=NOW)
    assert activity.observations == ()
    assert activity.skipped.total() == 0


def test_what_is_not_a_number_is_counted_or_ignored() -> None:
    out = ns.normalise_stream(
        [
            night(
                hr_average={"nested": 1},  # known, not a number: counted
                hr_min=True,  # a boolean is not a number: counted
                hr_max=61.5,  # a float never comes from the ingest: counted
                snoring="1e3",  # only plain decimal text: counted
                night_events=[1, 2],  # unknown, not a number: ignored
                label="deep",  # unknown string: ignored
                Bad_Name=3,  # unknown number whose name cannot be a key: counted
                **{"x" * 40: 1},  # too long for a key: counted
            )
        ],
        ns.SLEEP,
        now=NOW,
    )
    assert out.observations == ()
    assert out.skipped.invalid == 6
    assert out.skipped.total() == 6


def test_rows_the_table_cannot_hold_are_counted_never_written() -> None:
    out = ns.normalise_stream(
        [
            night(ref=1, hr_average="1000000000000.5"),  # beyond ±10^12
            night(ref=2, hr_average="0." + "0" * 31 + "1"),  # unit below -30
            replace(night(ref=3, hr_average=50), source_tz="Mars/Olympus"),
            replace(night(ref=4, hr_average=50), ts=datetime(2008, 12, 31, tzinfo=UTC)),
            replace(night(ref=5, hr_average=50), ts=datetime(2027, 3, 1, tzinfo=UTC)),
            day(date(2026, 10, 20), "Not/AZone", steps=1),
        ],
        ns.SLEEP,
        now=NOW,
    )
    assert out.observations == ()
    s = out.skipped
    assert (s.out_of_range, s.bad_zone, s.outside_partitions) == (2, 2, 2)


def test_the_same_night_from_two_connections_is_kept_once() -> None:
    first = night(hr_average=55)
    out = ns.normalise_stream([first, night(hr_average=58)], ns.SLEEP, now=NOW)
    (row,) = out.observations
    assert row.value == Decimal(55)  # canonical order, whatever order they arrived in
    assert out.skipped.duplicate_rows == 1
    # Two activity days on the same date, one per connection: also one row.
    on = date(2026, 10, 20)
    out = ns.normalise_stream([day(on, steps=10), day(on, steps=9)], ns.ACTIVITY, now=NOW)
    assert [r.value for r in out.observations] == [Decimal(10)]  # "10" sorts before "9"
    assert out.skipped.duplicate_rows == 1


def test_the_result_does_not_depend_on_input_order() -> None:
    records = [night(ref=n, hr_average=50 + n, waso=str(n)) for n in range(1, 30)] + [
        night(ref=None, hr_average=40)
    ]
    expected = ns.normalise_stream(records, ns.SLEEP, now=NOW)
    for seed in range(5):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)  # noqa: S311 - a seeded shuffle, not a secret
        assert ns.normalise_stream(shuffled, ns.SLEEP, now=NOW) == expected


def test_every_row_is_under_its_streams_prefix() -> None:
    for spec in (ns.SLEEP, ns.WORKOUT):
        rec = night(**{name: 1 for name in spec.known}, extra_one=2)
        for row in ns.normalise_stream([rec], spec, now=NOW).observations:
            assert ns.owned_key(spec.prefix, row.metric_key)
            assert row.source_type is None


def test_the_stream_fingerprint_follows_ids_hashes_stream_and_version() -> None:
    a = GroupRef(uuid.UUID(int=1), b"\x01" * 32, False)
    b = GroupRef(uuid.UUID(int=2), b"\x02" * 32, False)
    base = ns.stream_fingerprint("sleep_summary", [a, b])
    assert len(base) == 32
    assert ns.stream_fingerprint("sleep_summary", [b, a]) == base
    assert ns.stream_fingerprint("activity_daily", [a, b]) != base
    assert ns.stream_fingerprint("sleep_summary", [a]) != base
    assert (
        ns.stream_fingerprint("sleep_summary", [a, replace(b, payload_hash=b"\x03" * 32)]) != base
    )


def test_stored_bodies_are_read_defensively() -> None:
    assert ns.fields_of({"data": {"a": 1}}, "data") == (("a", 1),)
    assert ns.fields_of({"data": [1]}, "data") == ()
    assert ns.fields_of(None, "data") == ()
    assert ns.ref_of({"id": 5}) == 5
    assert ns.ref_of({"id": True}) is None
    assert ns.ref_of({"id": "5"}) is None
    assert ns.ref_of(None) is None
