"""The synthetic data generator: deterministic per seed, inside physiological ranges unless a
test asks for outliers, covering every meastype of findings section 2, and labelled synthetic.
Synthetic data only; nothing here reads real health data.
"""

import itertools
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import withingsstreams
from withingsdata import (
    ATTRIB_VALUES,
    MEASURE_RANGES,
    UNDOCUMENTED_ATTRIB,
    UNDOCUMENTED_MEASTYPE,
    SyntheticDataset,
    generate_dataset,
    in_range,
    real_value,
)
from withingsstreams import DAY, ECG_CLASSES, ECG_LIMIT_UV, ECG_MODELS, START, ecg_records, rng_for

TESTS = Path(__file__).parent
FINDINGS_MEASTYPES = {  # findings section 2, "Measure types", plus the per-foot ones it mentions
    1,
    4,
    5,
    6,
    8,
    9,
    10,
    11,
    12,
    54,
    71,
    73,
    76,
    77,
    88,
    91,
    123,
    130,
    135,
    136,
    137,
    138,
    139,
    147,
    148,
    151,
    155,
    158,
    159,
    167,
    168,
    169,
    170,
    173,
    174,
    175,
    196,
    197,
    198,
    204,
    205,
    226,
    227,
    229,
    248,
    249,
    251,
}
POSITIONS = {2, 3, 10, 11, 12}  # right arm, left arm, left leg, right leg, torso
SEEDS = (0, 1, 2, 3, 4)


def measures(ds: SyntheticDataset) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for group in ds.measuregrps:
        for m in group["measures"]:
            yield group, m


def test_the_same_seed_gives_the_same_dataset_and_another_seed_a_different_one() -> None:
    first, again = generate_dataset(3, days=30), generate_dataset(3, days=30)
    assert first.digest() == again.digest()
    assert first.to_dict() == again.to_dict()
    assert generate_dataset(4, days=30).digest() != first.digest()
    assert generate_dataset(3, days=31).digest() != first.digest()
    assert generate_dataset(3, days=30, quirks=True).digest() != first.digest()


@pytest.mark.parametrize("hash_seed", ["1", "2"])
def test_the_dataset_does_not_depend_on_the_interpreter_hash_seed(hash_seed: str) -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from withingsdata import generate_dataset; "
        "print(generate_dataset(7, days=20, quirks=True, outliers=2).digest())"
    )
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(TESTS)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == generate_dataset(7, days=20, quirks=True, outliers=2).digest()


@pytest.mark.parametrize("quirks", [False, True])
def test_every_value_stays_inside_the_physiological_ranges(quirks: bool) -> None:
    for seed in SEEDS:
        ds = generate_dataset(seed, days=90, quirks=quirks, ecg_count=6)
        for _, m in measures(ds):
            assert in_range(m), (seed, m, MEASURE_RANGES.get(m["type"]))
        assert ds.outliers == []


def test_outliers_appear_only_when_asked_for_and_are_listed() -> None:
    ds = generate_dataset(1, days=60, outliers=3)
    out_of_range = {
        (g["grpid"], m["type"])
        for g, m in measures(ds)
        if m["type"] in MEASURE_RANGES and not in_range(m)
    }
    assert out_of_range == set(ds.outliers)
    assert len(out_of_range) == 3
    assert all(
        real_value(m) > MEASURE_RANGES[m["type"]][1]
        for g, m in measures(ds)
        if (g["grpid"], m["type"]) in out_of_range
    )


def test_other_series_stay_inside_sensible_ranges() -> None:
    for seed in SEEDS:
        ds = generate_dataset(seed, days=60, ecg_count=6, sleep_series_nights=3, intraday_days=2)
        for a in ds.activities:
            assert 0 <= a["steps"] <= 30_000
            assert a["active"] == a["moderate"] + a["intense"]
            assert 30 <= a["hr_min"] <= a["hr_average"] <= a["hr_max"] <= 220
        for s in ds.sleep_summaries:
            d = s["data"]
            assert (
                d["total_sleep_time"]
                == d["lightsleepduration"] + d["deepsleepduration"] + d["remsleepduration"]
            )
            assert d["total_timeinbed"] >= d["total_sleep_time"] > 0
            assert 0 < d["sleep_efficiency"] <= 1
            assert 0 <= d["sleep_score"] <= 100
            assert s["startdate"] < s["enddate"]
        for sample in ds.intraday.values():
            assert 40 <= sample["heart_rate"] <= 200
            assert sample.get("spo2_auto", 95) <= 100
        for night in ds.sleep_series:
            for segment in night["series"]:
                assert all(30 <= v <= 200 for v in segment["hr"].values())
        assert ds.goals == {
            "steps": 10_000,
            "sleep": 28_800,
            "weight": {"value": 70_500, "unit": -3},
        }


def test_nothing_lies_in_the_future_of_the_fake_clock() -> None:
    ds = generate_dataset(2, days=20, ecg_count=4, sleep_series_nights=3)
    stamps = [g[k] for g in ds.measuregrps for k in ("date", "created", "modified") if k in g]
    stamps += [a["modified"] for a in ds.activities] + [s["modified"] for s in ds.sleep_summaries]
    stamps += [w["modified"] for w in ds.workouts] + list(ds.intraday)
    stamps += [e["list"][k] for e in ds.ecg for k in ("timestamp", "modified")]
    assert max(stamps) <= START
    assert min(stamps) >= START - 20 * DAY


@pytest.mark.parametrize("days", [1, 3, 10, 45])
def test_every_documented_meastype_appears_even_in_a_short_window(days: int) -> None:
    ds = generate_dataset(0, days=days, ecg_count=1)
    seen = {m["type"] for _, m in measures(ds)}
    assert seen >= FINDINGS_MEASTYPES
    assert UNDOCUMENTED_MEASTYPE not in seen


def test_attrib_values_and_segmental_positions_are_covered() -> None:
    attribs = {g["attrib"] for seed in SEEDS for g in generate_dataset(seed, days=90).measuregrps}
    assert attribs == set(ATTRIB_VALUES)
    for seed in SEEDS:
        ds = generate_dataset(seed, days=40)
        assert {g["attrib"] for g in ds.measuregrps} >= {0, 2, 4, 15}
        segmental = [(g, m) for g, m in measures(ds) if m["type"] in (173, 174, 175)]
        assert {m["position"] for _, m in segmental} == POSITIONS
        assert all(g["model_id"] == 10 for g, _ in segmental)  # Body Scan
        assert all("position" not in m for _, m in measures(ds) if m["type"] not in (173, 174, 175))
        nerve = {m["type"]: g["attrib"] for g, m in measures(ds) if m["type"] in (167, 196, 229)}
        assert nerve[167] == 15  # guided conditions
        assert {158, 159, 197, 198} <= {m["type"] for _, m in measures(ds)}


def test_the_series_are_irregular_and_sparse_with_gaps() -> None:
    ds = generate_dataset(1, days=60)
    weights = sorted(g["date"] for g, m in measures(ds) if m["type"] == 1 and g["category"] == 1)
    gaps = [b - a for a, b in itertools.pairwise(weights)]
    assert len(set(gaps)) > len(gaps) // 2  # not a regular schedule
    assert max(gaps) >= 4 * DAY  # a stretch without any weigh-in
    assert len({w // DAY for w in weights}) < 60  # not every day
    per_day: dict[int, int] = {}
    for w in weights:
        per_day[w // DAY] = per_day.get(w // DAY, 0) + 1
    assert any(n >= 2 for n in per_day.values()) or any(
        n >= 2 for seed in SEEDS for n in _per_day(generate_dataset(seed, days=90)).values()
    )
    assert len(ds.activities) < 60  # days the watch was not worn
    assert len(ds.sleep_summaries) < 59


def _per_day(ds: SyntheticDataset) -> dict[int, int]:
    out: dict[int, int] = {}
    for g, m in measures(ds):
        if m["type"] == 1 and g["category"] == 1:
            out[g["date"] // DAY] = out.get(g["date"] // DAY, 0) + 1
    return out


def _peaks(signal: list[int], hz: int) -> list[float]:
    """R peaks (seconds): local maxima over half of the largest value, 0.3 s apart."""
    threshold = 0.5 * max(signal)
    found: list[int] = []
    for i in range(1, len(signal) - 1):
        if signal[i] >= threshold and signal[i] >= signal[i - 1] and signal[i] > signal[i + 1]:
            if found and i - found[-1] < 0.3 * hz:
                if signal[i] > signal[found[-1]]:
                    found[-1] = i
                continue
            found.append(i)
    return [i / hz for i in found]


def test_ecg_signals_are_integer_microvolts_at_300_or_500_hz() -> None:
    records = ecg_records(5, count=30, start=START - 30 * DAY, end=START)
    assert {r["signal"]["sampling_frequency"] for r in records} == {300, 500}
    for record in records:
        entry, body = record["list"], record["signal"]
        hz, seconds = ECG_MODELS[entry["model"]]
        assert body["sampling_frequency"] == hz
        assert (
            len(body["signal"]) == hz * seconds
        )  # BPM Core 20 s at 500 Hz, Move ECG 30 s at 300 Hz
        assert all(isinstance(v, int) for v in body["signal"])
        assert max(abs(v) for v in body["signal"]) <= ECG_LIMIT_UV
        assert max(body["signal"]) > 500  # an R wave is there
        assert body["heart_rate"]["value"] == entry["heart_rate"]


def test_the_ecg_amplitude_is_clipped_to_a_physiological_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(withingsstreams, "ECG_LIMIT_UV", 400)
    clipped = withingsstreams.ecg_signal(rng_for(1, "clip"), hz=300, seconds=10, heart_rate=60)
    assert max(clipped) == 400  # the R wave reaches the limit
    assert min(clipped) >= -400


def test_ecg_classes_follow_the_documented_enums() -> None:
    records = ecg_records(5, count=60, start=START - 30 * DAY, end=START)
    details = {r["list"]["ecg"]["afib_details"] for r in records}
    assert len(details) >= 5
    assert details <= set(ECG_CLASSES)
    for record in records:
        ecg = record["list"]["ecg"]
        expected = {0: 0, 9: 0, 10: 0, 1: 1, 11: 1, 12: 1}.get(ecg["afib_details"], 2)
        assert ecg["afib"] == expected
        assert ("bloodpressure" in record["list"]) == (record["list"]["model"] == 44)


def test_the_ecg_rhythm_matches_its_classification() -> None:
    checked = {"sinus": 0, "afib": 0}
    for record in ecg_records(8, count=60, start=START - 30 * DAY, end=START):
        entry, body = record["list"], record["signal"]
        rhythm = ECG_CLASSES[entry["ecg"]["afib_details"]][2]
        if rhythm == "noisy":
            continue
        peaks = _peaks(body["signal"], body["sampling_frequency"])
        intervals = [b - a for a, b in itertools.pairwise(peaks)]
        rate = 60 * len(intervals) / (peaks[-1] - peaks[0])
        assert rate == pytest.approx(entry["heart_rate"], rel=0.15)
        mean = sum(intervals) / len(intervals)
        spread = (sum((i - mean) ** 2 for i in intervals) / len(intervals)) ** 0.5 / mean
        assert (spread > 0.1) == (rhythm == "afib"), (entry["ecg"], spread)
        checked[rhythm] += 1
    assert min(checked.values()) >= 3


def test_quirks_add_what_the_real_api_will_send_and_nothing_else_does() -> None:
    plain = generate_dataset(3, days=45, ecg_count=4, sleep_series_nights=2)
    ds = generate_dataset(3, days=45, ecg_count=4, sleep_series_nights=2, quirks=True)
    assert plain.quirks == []
    assert len(ds.quirks) >= 6
    for dataset, expected in ((plain, False), (ds, True)):
        groups = dataset.measuregrps
        found = {
            "undocumented attrib": any(g.get("attrib") == UNDOCUMENTED_ATTRIB for g in groups),
            "undocumented meastype": any(
                m["type"] == UNDOCUMENTED_MEASTYPE for _, m in measures(dataset)
            ),
            "unknown category": any(g.get("category") == 3 for g in groups),
            "missing optional group fields": any("comment" not in g for g in groups),
            "partial group": any("attrib" not in g and "measures" in g for g in groups),
            "unknown position": any(m.get("position") == 99 for _, m in measures(dataset)),
            "undocumented ecg model": any(e["list"]["model"] == 93 for e in dataset.ecg),
            "undocumented afib_details": any(
                e["list"]["ecg"].get("afib_details") == 99 for e in dataset.ecg
            ),
            "ecg without afib_details": any(
                "afib_details" not in e["list"]["ecg"] for e in dataset.ecg
            ),
            "unknown device": any(d["type"] == "Mystery Gadget" for d in dataset.devices),
            "unknown battery": any(d.get("battery") == "critical" for d in dataset.devices),
            "device without battery": any("battery" not in d for d in dataset.devices),
            "unknown sleep state": any(
                s["state"] == 42 for n in dataset.sleep_series for s in n["series"]
            ),
            "unknown workout": any(w["category"] == 9_999 for w in dataset.workouts),
            "partial activity": any("hr_average" not in a for a in dataset.activities),
            "unknown summary field": any(
                "new_metric_2027" in s["data"] for s in dataset.sleep_summaries
            ),
        }
        assert all(found.values()) if expected else not any(found.values()), found


def test_the_dataset_is_labelled_synthetic_and_serialises_to_json() -> None:
    ds = generate_dataset(1, days=10, ecg_count=2, quirks=True)
    document = ds.to_dict()
    assert document["synthetic"] is True
    assert document["seed"] == 1
    assert json.loads(json.dumps(document)) == document  # plain JSON, no objects
    assert all(len(d["deviceid"]) == 40 for d in ds.devices)  # derived from the seed
    assert generate_dataset(2, days=10).devices[0]["deviceid"] != ds.devices[0]["deviceid"]


def test_the_edges_of_the_arguments_work() -> None:
    assert (
        generate_dataset(0, days=1, ecg_count=0, sleep_series_nights=0, intraday_days=0).ecg == []
    )
    empty = generate_dataset(0, days=5, intraday_days=0, sleep_series_nights=0)
    assert empty.intraday == {}
    assert empty.sleep_series == []
    long = generate_dataset(0, days=5, sleep_series_nights=9)
    assert len(long.sleep_series) == len(long.sleep_summaries)
