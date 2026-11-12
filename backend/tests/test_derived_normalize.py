"""Normalisation, the pure part (#185): metric keys, exact values, provenance fields, quality
flags, skips and the fingerprint. No database. Synthetic measures only; no real data."""

import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ownvitals.analysis.quality import METRICS
from ownvitals.derived import metric_keys
from ownvitals.derived.metric_keys import metric_key
from ownvitals.derived.normalize import (
    ALGO_VERSION,
    GroupRef,
    SourceMeasure,
    fingerprint,
    normalise,
)
from ownvitals.derived.tables import METRIC_KEY_PATTERN
from withingsdata import MEASURE_RANGES, UNDOCUMENTED_MEASTYPE

NOW = datetime(2026, 11, 1, tzinfo=UTC)
T0 = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)
CONVERSION = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$")  # observations_conversion


def _m(grpid: int, type_: int, raw: int, unit: int = -3, **kw: object) -> SourceMeasure:
    base: dict[str, object] = {
        "measured_at": T0,
        "source_tz": "Europe/Stockholm",
        "attrib": 0,
        "position": None,
    }
    base.update(kw)
    return SourceMeasure(grpid=grpid, type=type_, value_raw=raw, unit=unit, **base)  # type: ignore[arg-type]


# Every meastype in the fake's fixtures (``withingsdata.MEASURE_RANGES`` plus the undocumented
# one) and the key it becomes. Pinned: a change here is a decision, not an accident.
FAKE_TABLE = {
    1: "weight", 4: "height", 5: "fat_free_mass", 6: "fat_ratio", 8: "fat_mass",
    9: "diastolic_bp", 10: "systolic_bp", 11: "heart_pulse", 12: "temperature",
    54: "spo2", 71: "body_temperature", 73: "skin_temperature", 76: "muscle_mass",
    77: "hydration", 88: "bone_mass", 91: "pulse_wave_velocity", 123: "vo2_max",
    130: "afib_ecg_result", 135: "qrs_duration", 136: "pr_interval", 137: "qt_interval",
    138: "qtc_interval", 139: "afib_ppg_result", 147: "meastype_147", 148: "meastype_148",
    151: "meastype_151", 155: "vascular_age", 158: "meastype_158", 159: "meastype_159",
    167: "nerve_health_score", 168: "extracellular_water", 169: "intracellular_water",
    170: "visceral_fat", 173: "segmental_fat_free_mass:unpositioned",
    174: "segmental_fat_mass:unpositioned", 175: "segmental_muscle_mass:unpositioned",
    196: "nerve_response_score", 197: "meastype_197", 198: "meastype_198",
    204: "meastype_204", 205: "meastype_205", 226: "basal_metabolic_rate",
    227: "metabolic_age", 229: "esc", 248: "meastype_248", 249: "meastype_249",
    251: "meastype_251",
}  # fmt: skip


def test_every_meastype_of_the_fake_maps_to_a_key_or_is_kept_as_unknown() -> None:
    assert set(FAKE_TABLE) == set(MEASURE_RANGES)
    pattern = re.compile(METRIC_KEY_PATTERN)
    for meastype, expected in FAKE_TABLE.items():
        found = metric_key(meastype)
        assert found.key == expected, meastype
        assert pattern.fullmatch(found.key)
        assert CONVERSION.fullmatch(metric_keys.conversion(-3, found.unit))
    assert UNDOCUMENTED_MEASTYPE not in FAKE_TABLE
    unknown = metric_key(UNDOCUMENTED_MEASTYPE)
    assert unknown.key == f"meastype_{UNDOCUMENTED_MEASTYPE}"
    assert metric_key(-5).key == "meastype_neg5"


def test_the_quality_table_and_the_key_table_agree() -> None:
    for spec in METRICS.values():
        found = metric_key(spec.meastype)
        assert found.key == spec.key
        assert found.analysed
    assert metric_key(4).analysed  # height has a quality row (#492)
    assert not metric_key(12).analysed
    assert not metric_key(175, 3).analysed


def test_segmental_types_are_keyed_on_position() -> None:
    assert metric_key(175, 3).key == "segmental_muscle_mass:left_arm"
    assert metric_key(175, 2).key == "segmental_muscle_mass:right_arm"
    assert metric_key(175, 10).key == "segmental_muscle_mass:left_leg"
    assert metric_key(175, 11).key == "segmental_muscle_mass:right_leg"
    assert metric_key(173, 12).key == "segmental_fat_free_mass:torso"
    assert metric_key(174, 13).key == "segmental_fat_mass:position_13"
    assert metric_key(174, 123456).key == "segmental_fat_mass:position_other"
    pattern = re.compile(METRIC_KEY_PATTERN)
    for position in (None, *range(0, 20), -1, 10**9):
        assert pattern.fullmatch(metric_key(175, position).key)


def test_a_group_with_all_segments_keeps_every_position() -> None:
    measures = [_m(7, 175, 3000 + p, position=p) for p in (2, 3, 10, 11)]
    measures += [_m(7, 173, 2000 + p, position=p) for p in (2, 3)]
    result = normalise(measures, now=NOW)
    keys = sorted(o.metric_key for o in result.observations)
    assert keys == [
        "segmental_fat_free_mass:left_arm",
        "segmental_fat_free_mass:right_arm",
        "segmental_muscle_mass:left_arm",
        "segmental_muscle_mass:left_leg",
        "segmental_muscle_mass:right_arm",
        "segmental_muscle_mass:right_leg",
    ]
    assert result.skipped.total() == 0
    assert len({o.natural_key for o in result.observations}) == 6


def test_the_value_is_exact_and_the_raw_value_unit_and_conversion_are_kept() -> None:
    result = normalise([_m(1, 1, 80_050), _m(1, 11, 61, 0), _m(1, 76, 1, -30)], now=NOW)
    by_key = {o.metric_key: o for o in result.observations}
    weight = by_key["weight"]
    assert weight.value == Decimal("80.050")
    assert isinstance(weight.value, Decimal)
    assert (weight.source_value_raw, weight.source_unit) == (80_050, -3)
    assert weight.conversion == "e-3:kg"
    assert weight.ts == weight.source_ts == T0
    assert weight.source_tz == "Europe/Stockholm"
    assert (weight.source_grpid, weight.source_type, weight.source_position) == (1, 1, None)
    assert by_key["heart_pulse"].conversion == "e0:bpm"
    assert by_key["muscle_mass"].value == Decimal("1E-30")  # not rounded


def test_unknown_types_are_kept_with_an_unknown_unit() -> None:
    result = normalise([_m(1, 4242, 17, 0)], now=NOW)
    (row,) = result.observations
    assert row.metric_key == "meastype_4242"
    assert row.conversion == "e0:unknown"
    assert row.value == 17
    assert row.quality_flags == ()


def test_attrib_one_is_flagged_unconfirmed_for_every_metric() -> None:
    result = normalise([_m(1, 1, 70_000, attrib=1), _m(1, 4, 175, -2, attrib=1)], now=NOW)
    flags = {o.metric_key: o.quality_flags for o in result.observations}
    assert flags["weight"] == ("unconfirmed",)
    assert flags["height"] == ("unconfirmed",)
    other = normalise([_m(2, 1, 70_000, attrib=0), _m(3, 1, 70_000, attrib=None)], now=NOW)
    assert all(o.quality_flags == () for o in other.observations)


def test_implausible_and_jump_flags_come_from_assess() -> None:
    day = timedelta(hours=1)
    measures = [
        _m(1, 1, 70_000, measured_at=T0),
        _m(2, 1, 76_500, measured_at=T0 + day),  # +6.5 kg within 24 h: a jump
        _m(3, 1, 5_000, measured_at=T0 + 2 * day),  # 5 kg: outside 10..300
    ]
    flags = {o.source_grpid: o.quality_flags for o in normalise(measures, now=NOW).observations}
    assert flags == {1: (), 2: ("jump",), 3: ("implausible",)}


def test_deliberate_repeats_are_kept_and_a_repeated_row_is_counted() -> None:
    repeats = [_m(1, 1, 70_000), _m(2, 1, 70_000)]  # same instant and value, two groups
    result = normalise(repeats, now=NOW)
    assert len(result.observations) == 2
    assert all(o.quality_flags == () for o in result.observations)
    again = normalise([*repeats, _m(1, 1, 99_000)], now=NOW)  # grpid 1, type 1 twice
    assert len(again.observations) == 2
    assert again.skipped.duplicate_rows == 1
    # the first in canonical order wins: the same whichever way round they arrive
    kept = [o.source_value_raw for o in again.observations if o.source_grpid == 1]
    assert kept == [70_000]


def test_rows_the_table_cannot_hold_are_counted_not_dropped_silently() -> None:
    measures = [
        _m(1, 1, 9 * 10**18, 0),  # beyond 10^12
        _m(2, 1, 1, 31),  # unit outside -30..30
        _m(3, 1, 70_000, source_tz="Not/AZone"),
        _m(4, 1, 70_000, measured_at=datetime(2008, 12, 31, tzinfo=UTC)),
        _m(5, 1, 70_000, measured_at=NOW + timedelta(days=200)),
        _m(6, 1, 70_000),
    ]
    result = normalise(measures, now=NOW)
    assert [o.source_grpid for o in result.observations] == [6]
    skipped = result.skipped
    assert (skipped.out_of_range, skipped.bad_zone, skipped.outside_partitions) == (2, 1, 2)


def test_the_result_does_not_depend_on_the_order_of_the_input() -> None:
    rng = random.Random(185)  # noqa: S311 - a seeded shuffle, not a secret
    measures = [
        _m(g, t, 70_000 + 13 * g + t, measured_at=T0 + timedelta(hours=g), position=p)
        for g in range(1, 15)
        for t, p in ((1, None), (11, None), (175, 3), (175, 2), (4242, None))
    ]
    first = normalise(measures, now=NOW)
    for _ in range(3):
        shuffled = list(measures)
        rng.shuffle(shuffled)
        assert normalise(shuffled, now=NOW) == first
    assert normalise([], now=NOW).observations == ()


def test_the_algorithm_version_is_pinned_to_the_quality_gates() -> None:
    assert ALGO_VERSION == "normalize/1"
    assert re.fullmatch(r"^[a-z][a-z0-9_-]{0,31}/[0-9]{1,6}$", ALGO_VERSION)  # the table's check


def test_the_fingerprint_follows_the_ids_hashes_and_deleted_marks() -> None:
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    refs = [GroupRef(a, b"\x01" * 32, False), GroupRef(b, b"\x02" * 32, False)]
    base = fingerprint(refs)
    assert len(base) == 32
    assert fingerprint(list(reversed(refs))) == base
    assert fingerprint(refs[:1]) != base
    assert fingerprint([refs[0], GroupRef(b, b"\x03" * 32, False)]) != base
    assert fingerprint([refs[0], GroupRef(b, b"\x02" * 32, True)]) != base  # marked, not omitted
    assert fingerprint([]) != fingerprint([GroupRef(a, b"", False)])
