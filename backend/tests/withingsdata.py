"""Seeded synthetic Withings data: the dataset the fake serves and the fixtures are made of.

**Everything is synthetic**: invented from a seed, labelled ``synthetic: true`` (the dataset's
``to_dict``) and never derived from real health data (AGENTS.md). The same seed gives the same
dataset; values stay inside ``MEASURE_RANGES`` (broad human ranges, not clinical reference
ranges) unless ``outliers`` asks for out-of-range values, which are then listed in
``SyntheticDataset.outliers``. Standard library only (importable by the e2e service).

What the generator produces (``docs/withings-findings.md`` section 2): irregular, sparse body
series with multi-day gaps; every meastype of the findings table including Body Scan segmental
rows (``position``), nerve health, ECG intervals and U-Scan biomarkers; every documented
``attrib``; ECG signals (integer microvolts at 300 or 500 Hz); sleep, activity, workouts and
intraday series. ``quirks=True`` adds undocumented enum values, missing optional fields and
partial records, because the real API will send them (PLAN 4.2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from withingsstreams import (
    DAY,
    START,
    TIMEZONE,
    activity_day,
    clamp,
    device_id,
    ecg_records,
    hash_device_id,
    intraday_day,
    rng_for,
    sleep_night,
    workouts_for_day,
)

# Broad physiological range per meastype, in real units (value * 10**unit). Nothing is clinical.
MEASURE_RANGES: dict[int, tuple[float, float]] = {
    1: (30, 250), 4: (0.5, 2.5), 5: (15, 150), 6: (3, 60), 8: (1, 150), 9: (30, 140),
    10: (60, 250), 11: (30, 220), 12: (34, 42), 54: (70, 100), 71: (34, 42), 73: (25, 40),
    76: (10, 100), 77: (10, 80), 88: (0.5, 6), 91: (3, 25), 123: (10, 80), 130: (0, 13),
    135: (40, 200), 136: (80, 400), 137: (250, 600), 138: (300, 600), 139: (0, 1),
    147: (4.5, 8.5), 148: (1.0, 1.04), 151: (0, 1), 155: (15, 100), 158: (0, 100),
    159: (0, 100), 167: (0, 100), 168: (3, 40), 169: (5, 50), 170: (1, 30), 173: (0.1, 40),
    174: (0.1, 40), 175: (0.1, 40), 196: (0, 100), 197: (0, 100), 198: (0, 100),
    204: (0, 10), 205: (0, 100), 226: (800, 4500), 227: (10, 100), 229: (0, 100),
    248: (0, 500), 249: (10, 400), 251: (0, 2),
}  # fmt: skip
MEASURE_UNITS: dict[int, int] = {
    1: -3,
    4: -2,
    5: -3,
    6: -3,
    8: -3,
    12: -2,
    71: -2,
    73: -2,
    76: -3,
    77: -3,
    88: -3,
    91: -1,
    123: -1,
    147: -1,
    148: -3,
    168: -3,
    169: -3,
    173: -3,
    174: -3,
    175: -3,
    204: -1,
    205: -1,
    251: -2,
}  # fmt: skip  (every other meastype has unit 0)
UNDOCUMENTED_MEASTYPE = (
    9_999  # a meastype the findings table does not list ("new ones keep appearing")
)
ATTRIB_VALUES = (0, 1, 2, 4, 5, 7, 8, 15)  # findings section 2
UNDOCUMENTED_ATTRIB = 99

# Segmental positions (findings section 2) and the share of the total each segment carries.
SEGMENTS: dict[int, tuple[str, float, float, float]] = {  # position: (name, ffm, fat, muscle)
    12: ("torso", 0.50, 0.55, 0.42),
    3: ("left arm", 0.065, 0.07, 0.075),
    2: ("right arm", 0.065, 0.07, 0.075),
    10: ("left leg", 0.185, 0.155, 0.215),
    11: ("right leg", 0.185, 0.155, 0.215),
}
MODEL_NAMES = {
    10: "Body Scan",
    44: "BPM Core",
    91: "Move ECG",
    93: "ScanWatch",
    63: "Aura Sensor V2",
}
NERVE_VARIANTS = ("score_167", "score_196", "esc_229")


def unit_of(meastype: int) -> int:
    return MEASURE_UNITS.get(meastype, 0)


def real_value(measure: dict[str, Any]) -> float:
    """``value x 10**unit``, the real value of a measure."""
    return measure["value"] * 10 ** measure["unit"]


def in_range(measure: dict[str, Any]) -> bool:
    """Whether a generated measure lies in its physiological range (to the rounding of its
    unit). Meastypes without a range (undocumented ones) count as in range."""
    bounds = MEASURE_RANGES.get(measure["type"])
    if bounds is None:
        return True
    slack = 10 ** measure["unit"] / 2 + 1e-9
    return bounds[0] - slack <= real_value(measure) <= bounds[1] + slack


def measure(meastype: int, real: float, *, position: int | None = None) -> dict[str, Any]:
    """A measure object for ``real`` (clamped to the range of the type), as the API sends it."""
    low, high = MEASURE_RANGES[meastype]
    unit = unit_of(meastype)
    out: dict[str, Any] = {
        "value": round(clamp(real, low, high) * 10**-unit),
        "type": meastype,
        "unit": unit,
    }
    if position is not None:
        out["position"] = position
    return out


@dataclass
class SyntheticDataset:
    """One synthetic user's data, in the shapes of the API (so the fake can serve it as is)."""

    seed: int
    timezone: str = TIMEZONE
    synthetic: bool = True
    devices: list[dict[str, Any]] = field(default_factory=list)
    goals: dict[str, Any] = field(default_factory=dict)
    measuregrps: list[dict[str, Any]] = field(default_factory=list)
    ecg: list[dict[str, Any]] = field(default_factory=list)  # {"list": entry, "signal": body}
    activities: list[dict[str, Any]] = field(default_factory=list)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    sleep_summaries: list[dict[str, Any]] = field(default_factory=list)
    sleep_series: list[dict[str, Any]] = field(default_factory=list)  # one body per night
    intraday: dict[int, dict[str, Any]] = field(default_factory=dict)
    outliers: list[tuple[int, int]] = field(default_factory=list)  # (grpid, meastype)
    quirks: list[str] = field(default_factory=list)  # what quirks=True added
    deleted_grpids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["intraday"] = {str(k): v for k, v in self.intraday.items()}
        out["outliers"] = [list(o) for o in self.outliers]
        return out

    def digest(self) -> str:
        """A hash of the whole dataset: equal seeds give equal digests."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


# ------------------------------------------------------------------------------ measure events


def _group(
    t: int, measures: list[dict[str, Any]], *, seed: int, model_id: int | None, attrib: int = 0
) -> dict[str, Any]:
    group: dict[str, Any] = {
        "grpid": 0,
        "attrib": attrib,
        "date": t,
        "created": t + 30,
        "modified": t + 30,
        "category": 1,
        "measures": measures,
        "comment": "",
    }
    if model_id is not None:
        device = device_id(seed, model_id)
        group.update(deviceid=device, hash_deviceid=hash_device_id(device))
        group.update(model=MODEL_NAMES[model_id], model_id=model_id)
    return group


def _body_scan(
    rng: Any,
    seed: int,
    t: int,
    weight: float,
    prof: dict[str, float],
    full: bool,
    segmental: bool | None = None,
) -> dict[str, Any]:
    fat_pct = clamp(prof["fat_pct"] + (weight - prof["weight"]) * 0.4 + rng.gauss(0, 0.6), 4, 55)
    fat = weight * fat_pct / 100
    ffm = weight - fat
    water = ffm * 0.73
    measures = [measure(1, weight)]
    if full:
        measures += [
            measure(5, ffm), measure(6, fat_pct), measure(8, fat),
            measure(76, ffm * 0.55), measure(77, water), measure(88, 3.0 + rng.gauss(0, 0.05)),
            measure(168, water * 0.4), measure(169, water * 0.6),
            measure(170, 3 + fat_pct * 0.2 + rng.gauss(0, 0.5)),
            measure(226, 370 + 21.6 * ffm), measure(227, prof["age"] + rng.gauss(0, 4)),
            measure(155, prof["age"] + rng.gauss(0, 6)), measure(91, 6 + prof["age"] * 0.06),
            measure(11, rng.gauss(64, 6)),
        ]  # fmt: skip
        if rng.random() < 0.6 if segmental is None else segmental:  # not every scan has segments
            for position, (_, ffm_share, fat_share, muscle_share) in SEGMENTS.items():
                measures += [
                    measure(173, ffm * ffm_share, position=position),
                    measure(174, fat * fat_share, position=position),
                    measure(175, ffm * 0.55 * muscle_share, position=position),
                ]
    attrib = rng.choice((0, 0, 0, 1, 8)) if full else rng.choice((0, 1, 2, 2))
    return _group(t, measures, seed=seed, model_id=10 if attrib != 2 else None, attrib=attrib)


def _nerve(rng: Any, seed: int, t: int, variant: str) -> dict[str, Any]:
    if variant == "score_167":
        measures = [measure(167, rng.uniform(20, 95))]
        attrib = 15  # guided conditions (Nerve Health guided scan)
    elif variant == "score_196":
        measures = [measure(196, rng.uniform(20, 95))]
        attrib = 0
    else:  # per-foot values seen in withings-sync, not in the current spec
        measures = [measure(229, rng.uniform(30, 95))]
        measures += [measure(m, rng.uniform(20, 95)) for m in (158, 159, 197, 198)]
        attrib = 0
    return _group(t, measures, seed=seed, model_id=10, attrib=attrib)


def _urine(rng: Any, seed: int, t: int) -> dict[str, Any]:
    ca, cr = rng.uniform(20, 300), rng.uniform(40, 300)
    measures = [
        measure(147, rng.uniform(5.0, 8.0)), measure(148, rng.uniform(1.005, 1.03)),
        measure(151, rng.choice((0, 0, 0, 1))), measure(204, rng.uniform(0, 3)),
        measure(205, rng.uniform(0, 60)), measure(248, ca), measure(249, cr),
        measure(251, ca / cr),
    ]  # fmt: skip
    return _group(t, measures, seed=seed, model_id=None)  # the U-Scan model id is not documented


def _measure_events(seed: int, days: int, end: int) -> list[dict[str, Any]]:
    """Irregular, sparse measure groups: gaps of several days, bursts, mixed sources."""
    rng = rng_for(seed, "measures")
    first = end - days * DAY
    prof = {
        "age": float(rng.randint(25, 70)),
        "weight": rng.uniform(55, 110),
        "fat_pct": rng.uniform(14, 36),
        "trend": rng.uniform(-0.04, 0.04),
    }
    gaps: set[int] = set()
    for _ in range(max(1, days // 20)):
        start = rng.randrange(days)
        gaps.update(range(start, start + rng.randint(4, 14)))
    walk, groups = 0.0, []
    forced: dict[str, bool] = dict.fromkeys(("weigh", "seg", "bp", "temp", "uscan", "vo2"), False)
    for variant in NERVE_VARIANTS:
        forced[variant] = False
    for day in range(days):
        walk = 0.9 * walk + rng.gauss(0, 0.35)
        last = day >= days - 1
        if day in gaps and not last:
            continue
        base = first + day * DAY
        weight = prof["weight"] + prof["trend"] * day + walk

        def at(hour: float, *, base: int = base) -> int:
            return base + int(clamp(hour, 4, 23.5) * 3600) + rng.randint(0, 59)

        need_segments = last and not forced["seg"]
        if rng.random() < 0.6 or (last and not forced["weigh"]) or need_segments:
            full = rng.random() < 0.75 or (last and not forced["weigh"]) or need_segments
            scan = _body_scan(
                rng,
                seed,
                at(rng.gauss(7.5, 1.5)),
                weight,
                prof,
                full,
                True if need_segments else None,
            )
            groups.append(scan)
            forced["weigh"] = forced["weigh"] or full
            forced["seg"] = forced["seg"] or any("position" in m for m in scan["measures"])
            if rng.random() < 0.15:  # a second weigh-in the same day
                groups.append(
                    _body_scan(rng, seed, at(rng.gauss(19, 1.5)), weight + 0.3, prof, False)
                )
        if rng.random() < 0.35 or (last and not forced["bp"]):
            for hour in (8.0, 20.0)[: rng.randint(1, 2)]:
                bp = [measure(10, rng.gauss(122, 12)), measure(9, rng.gauss(78, 8))]
                bp.append(measure(11, rng.gauss(66, 8)))
                groups.append(
                    _group(at(hour), bp, seed=seed, model_id=44, attrib=rng.choice((0, 5)))
                )
            forced["bp"] = True
        for variant in NERVE_VARIANTS:
            if rng.random() < 0.05 or (last and not forced[variant]):
                groups.append(_nerve(rng, seed, at(rng.gauss(7.6, 0.5)), variant))
                forced[variant] = True
        if rng.random() < 0.3 or (last and not forced["temp"]):
            night = [measure(54, rng.gauss(96, 1.5)), measure(73, rng.gauss(33.5, 0.6))]
            night.append(measure(71, rng.gauss(36.6, 0.3)))
            groups.append(_group(at(3.0), night, seed=seed, model_id=93, attrib=0))
            forced["temp"] = True
            if rng.random() < 0.4 or last:  # a manual thermometer reading
                reading = [measure(12, rng.gauss(36.7, 0.3))]
                groups.append(_group(at(8.0), reading, seed=seed, model_id=None, attrib=2))
        if rng.random() < 0.1 or (last and not forced["vo2"]):
            extra = [measure(123, rng.uniform(25, 55)), measure(139, rng.choice((0, 0, 0, 1)))]
            groups.append(_group(at(18.0), extra, seed=seed, model_id=93, attrib=7))
            forced["vo2"] = True
        if rng.random() < 0.08 or (last and not forced["uscan"]):
            groups.append(_urine(rng, seed, at(7.0)))
            forced["uscan"] = True
    height = _group(
        first + 60, [measure(4, rng.uniform(1.55, 1.95))], seed=seed, model_id=None, attrib=4
    )
    goal = _group(first + 120, [measure(1, prof["weight"] - 5)], seed=seed, model_id=None, attrib=2)
    goal["category"] = 2  # user objective
    return [height, goal, *groups]


def _ecg_groups(seed: int, ecg: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The measure groups that go with the ECG recordings: the AFib result (meastype 130,
    the classification) and the intervals (135 to 138). Links ``heart get`` to its group."""
    rng = rng_for(seed, "ecg-measures")
    groups = []
    for record in ecg:
        entry = record["list"]
        model_id = entry["model"] if entry["model"] in MODEL_NAMES else 93
        qrs = rng.uniform(75, 110)
        detail = entry["ecg"].get("afib_details")
        classification = detail if detail in range(14) else entry["ecg"]["afib"]
        measures = [
            measure(130, classification),
            measure(135, qrs), measure(136, rng.uniform(120, 200)),
            measure(137, rng.uniform(350, 450)), measure(138, rng.uniform(380, 460)),
        ]  # fmt: skip
        groups.append(_group(entry["timestamp"], measures, seed=seed, model_id=model_id))
        record["_group"] = groups[-1]
    return groups


def _devices(seed: int, quirks: bool) -> list[dict[str, Any]]:
    rng = rng_for(seed, "devices")
    rows = [
        (10, "Scale", "high"), (44, "Blood Pressure Monitor", "medium"),
        (93, "Activity Tracker", "low"), (63, "Sleep Monitor", "high"),
    ]  # fmt: skip
    devices = []
    for model_id, kind, battery in rows:
        device = device_id(seed, model_id)
        devices.append(
            {
                "type": kind,
                "model": MODEL_NAMES[model_id],
                "model_id": model_id,
                "battery": battery,
                "deviceid": device,
                "hash_deviceid": hash_device_id(device),
                "timezone": TIMEZONE,
                "first_session_date": START - 400 * DAY + rng.randint(0, DAY),
                "last_session_date": START - rng.randint(0, 3 * DAY),
            }
        )
    if quirks:
        devices.append(
            {"type": "Mystery Gadget", "model": "Future Thing", "model_id": 9_999,
             "deviceid": device_id(seed, 9_999), "battery": "critical"}
        )  # fmt: skip
        del devices[1]["battery"]  # missing optional field
    return devices


def _apply_quirks(ds: SyntheticDataset) -> None:
    """Undocumented enum values, missing optional fields and partial records (PLAN 4.2)."""
    groups = ds.measuregrps
    if len(groups) < 6:
        return
    groups[1]["attrib"] = UNDOCUMENTED_ATTRIB
    groups[2]["measures"].append({"value": 4_242, "type": UNDOCUMENTED_MEASTYPE, "unit": -1})
    groups[3]["category"] = 3
    for key in ("deviceid", "hash_deviceid", "model", "model_id", "comment"):
        groups[4].pop(key, None)  # optional fields missing
    last = groups[5]
    ds.measuregrps[5] = {k: last[k] for k in ("grpid", "date", "measures")}  # a partial record
    for m in groups[0]["measures"][:1]:
        m["position"] = 99
    ds.quirks += [
        "attrib=99", "meastype=9999", "category=3", "missing optional group fields",
        "partial group", "position=99",
    ]  # fmt: skip


def _outliers(ds: SyntheticDataset, count: int) -> None:
    rng = rng_for(ds.seed, "outliers")
    candidates = [
        (g, m) for g in ds.measuregrps for m in g["measures"]
        if m["type"] in (1, 6, 9, 10, 11, 54, 12) and g.get("category") == 1
    ]  # fmt: skip
    for g, m in rng.sample(candidates, min(count, len(candidates))):
        low, high = MEASURE_RANGES[m["type"]]
        m["value"] = round((high + (high - low) * 0.5) * 10 ** -m["unit"])
        ds.outliers.append((g["grpid"], m["type"]))


def generate_dataset(
    seed: int = 0,
    *,
    days: int = 45,
    end: int = START,
    outliers: int = 0,
    quirks: bool = False,
    ecg_count: int = 4,
    sleep_series_nights: int = 2,
    intraday_days: int = 1,
) -> SyntheticDataset:
    """A synthetic user covering the ``days`` days before ``end``. Deterministic per ``seed`` and
    arguments. ``outliers`` puts that many out-of-range values into ordinary series;
    ``quirks`` adds undocumented enum values, missing optional fields and partial records."""
    events = _measure_events(seed, days, end)
    first = end - days * DAY
    ds = SyntheticDataset(seed=seed, devices=_devices(seed, quirks))
    ds.goals = {"steps": 10_000, "sleep": 28_800, "weight": {"value": 70_500, "unit": -3}}
    ds.ecg = ecg_records(seed, count=ecg_count, start=first, end=end - 120, quirks=quirks)
    events += _ecg_groups(seed, ds.ecg)
    events.sort(key=lambda g: (g["date"], g["measures"][0]["type"]))
    for number, group in enumerate(events, start=1):
        group["grpid"] = 100_000 + number
    for record in ds.ecg:
        record["signal"]["heart_rate"]["grpid"] = record.pop("_group")["grpid"]
    ds.measuregrps = events
    rng = rng_for(seed, "wear")
    for d in range(days):
        day = first + d * DAY
        if rng.random() < 0.9:  # some days the watch is not worn
            ds.activities.append(
                activity_day(seed, day, cap=end - 1, quirks=quirks, index=len(ds.activities))
            )
        ds.workouts += workouts_for_day(seed, day, quirks=quirks, force=quirks and d == days // 2)
        if d < days - 1 and rng.random() < 0.85:  # a night ends the next day: none after the last
            series = days - 2 - d < sleep_series_nights
            summary, body = sleep_night(seed, d, day, quirks=quirks, series=series)
            ds.sleep_summaries.append(summary)
            if body is not None:
                ds.sleep_series.append(body)
    for d in range(max(0, days - intraday_days), days):
        ds.intraday.update(intraday_day(seed, first + d * DAY))
    if quirks:
        _apply_quirks(ds)
    if outliers:
        _outliers(ds, outliers)
    return ds
