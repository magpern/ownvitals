"""Seeded synthetic series for the Withings fake: ECG, sleep, activity, workouts, intraday.

Everything here is **synthetic**: invented from a seed, never real health data (AGENTS.md). Shapes
follow the public Withings API (``docs/withings-findings.md`` section 2 and the public OpenAPI
file); values stay inside broad physiological ranges. Standard library only, so the e2e service
can import it without the test suite. ``withingsdata`` assembles these into a dataset.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import UTC, datetime
from typing import Any, Literal

Rhythm = Literal["sinus", "afib", "noisy"]

START = 1_793_404_800  # 2026-10-31T00:00:00Z: where the fake's clock starts; data lies before it
DAY = 86_400
TIMEZONE = "UTC"

# Public spec: BPM Core records 20 s at 500 Hz, Move ECG 30 s at 300 Hz (heart v2 get).
ECG_MODELS: dict[int, tuple[int, int]] = {44: (500, 20), 91: (300, 30)}
ECG_UNDOCUMENTED_MODEL = 93  # ScanWatch: in the spec's text, absent from the heart model enum
ECG_LIMIT_UV = 5_000  # the signal is clipped here: a physiological single-lead amplitude

# (afib, afib_details, heart-rate range, rhythm) per classification, from findings section 2.
ECG_CLASSES: dict[int, tuple[int, tuple[int, int], Rhythm]] = {
    0: (0, (55, 95), "sinus"),
    1: (1, (60, 120), "afib"),
    2: (2, (55, 95), "sinus"),
    5: (2, (55, 95), "noisy"),
    6: (2, (36, 48), "sinus"),
    7: (2, (105, 150), "sinus"),
    9: (0, (55, 95), "sinus"),
    10: (0, (101, 140), "sinus"),
    11: (1, (60, 100), "afib"),
    12: (1, (101, 150), "afib"),
    13: (2, (55, 95), "noisy"),
}
ECG_CLASS_WEIGHTS = (30, 4, 3, 2, 1, 1, 14, 2, 3, 2, 1)

WORKOUT_CATEGORIES = (1, 2, 6, 7)  # walk, run, bicycling, swimming
UNDOCUMENTED_WORKOUT_CATEGORY = 9_999
SLEEP_STATES = (0, 1, 2, 3)  # awake, light, deep, rem
UNDOCUMENTED_SLEEP_STATE = 42


def rng_for(seed: int, name: str) -> random.Random:
    """A generator whose stream depends only on the seed and the stream name."""
    return random.Random(f"ownvitals-synthetic/{seed}/{name}")  # noqa: S311  (not a secret)


def device_id(seed: int, model_id: int) -> str:
    return hashlib.sha256(f"synthetic-device/{seed}/{model_id}".encode()).hexdigest()[:40]


def hash_device_id(device: str) -> str:
    return hashlib.sha256(f"synthetic-hash/{device}".encode()).hexdigest()[:40]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ymd(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------- ECG


def ecg_signal(
    rng: random.Random,
    *,
    hz: int,
    seconds: int,
    heart_rate: int,
    rhythm: Rhythm = "sinus",
) -> list[int]:
    """A single-lead ECG in integer microvolts: P, Q, R, S and T waves as Gaussian bumps at
    the beat times, plus baseline wander and noise. ``afib`` has irregular R-R intervals and no
    P wave; ``noisy`` buries the complexes in noise."""
    count = hz * seconds
    mean_rr = 60.0 / heart_rate
    spread = 0.22 if rhythm == "afib" else 0.03
    beats: list[float] = []
    t = rng.uniform(0.2, 0.8)
    while t < seconds + 0.5:
        beats.append(t)
        t += max(0.3, rng.gauss(mean_rr, mean_rr * spread))
    # (offset from the R peak in s, width in s, amplitude in uV)
    waves = ((-0.20, 0.025, 120.0), (-0.04, 0.010, -110.0), (0.0, 0.012, 1000.0))
    waves += ((0.04, 0.012, -220.0), (0.26, 0.050, 300.0))
    sig = [0.0] * count
    for beat in beats:
        for offset, width, amp in waves:
            if rhythm == "afib" and offset == -0.20:
                continue
            centre = beat + offset
            low = max(0, int((centre - 4 * width) * hz))
            high = min(count, int((centre + 4 * width) * hz) + 1)
            for i in range(low, high):
                x = (i / hz - centre) / width
                sig[i] += amp * math.exp(-0.5 * x * x)
    noise_sd = {"sinus": 12.0, "afib": 20.0, "noisy": 140.0}[rhythm]
    wander = rng.uniform(20, 60)
    wander_hz = rng.uniform(0.15, 0.35)
    phase = rng.uniform(0, 2 * math.pi)
    out: list[int] = []
    for i in range(count):
        v = sig[i] + wander * math.sin(2 * math.pi * wander_hz * i / hz + phase)
        if rhythm == "afib":
            v += 25 * math.sin(2 * math.pi * 7.0 * i / hz + phase)
        v += rng.gauss(0, noise_sd)
        out.append(round(clamp(v, -ECG_LIMIT_UV, ECG_LIMIT_UV)))
    return out


def ecg_records(
    seed: int, *, count: int, start: int, end: int, quirks: bool = False
) -> list[dict[str, Any]]:
    """``count`` ECG recordings between ``start`` and ``end``, oldest first. Each record holds
    the ``heart list`` entry (``list``) and the ``heart get`` body (``signal``)."""
    rng = rng_for(seed, "ecg")
    classes = list(ECG_CLASSES)
    records: list[dict[str, Any]] = []
    times = sorted(int(rng.uniform(start, end)) for _ in range(count))
    for index, timestamp in enumerate(times):
        model = rng.choice(list(ECG_MODELS))
        if quirks and index == 0:
            model = ECG_UNDOCUMENTED_MODEL
        hz, seconds = ECG_MODELS.get(model, (300, 30))
        detail = rng.choices(classes, weights=ECG_CLASS_WEIGHTS)[0]
        afib, (hr_low, hr_high), rhythm = ECG_CLASSES[detail]
        heart_rate = rng.randint(hr_low, hr_high)
        signal = ecg_signal(rng, hz=hz, seconds=seconds, heart_rate=heart_rate, rhythm=rhythm)
        device = device_id(seed, model)
        entry: dict[str, Any] = {
            "deviceid": device,
            "model": model,
            "ecg": {"signalid": 7_000 + index, "afib": afib, "afib_details": detail},
            "heart_rate": heart_rate,
            "modified": timestamp + 60,
            "timestamp": timestamp,
        }
        if model == 44:
            entry["bloodpressure"] = {
                "diastole": rng.randint(60, 90),
                "systole": rng.randint(100, 140),
            }
        body: dict[str, Any] = {
            "signal": signal,
            "sampling_frequency": hz,
            "wearposition": 3 if model == 44 else 1,
            "model": model,
            "heart_rate": {"grpid": 0, "value": heart_rate, "date": timestamp, "is_deleted": False},
        }
        if quirks and index == 0:
            entry["ecg"]["afib_details"] = 99  # undocumented enum value
            body["wearposition"] = 99
        if quirks and index == 1:
            del entry["ecg"]["afib_details"]  # missing optional field
        records.append({"list": entry, "signal": body})
    return records


# --------------------------------------------------------------------------------------- sleep


def sleep_night(
    seed: int, night: int, day_start: int, *, quirks: bool = False, series: bool = False
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One night starting the evening of ``day_start``: the ``getsummary`` entry and, with
    ``series``, the ``sleep get`` body (segments keyed by state, ``hr``/``rr``/... by timestamp)."""
    rng = rng_for(seed, f"sleep/{night}")
    model_id = rng.choice((63, 93))  # Aura Sensor V2 (model 32) or ScanWatch (model 16)
    model = 32 if model_id == 63 else 16
    start = day_start + int(rng.gauss(22.5, 0.9) * 3600)
    in_bed = int(rng.uniform(6.0, 9.5) * 3600)
    latency = int(rng.uniform(300, 1800))
    wake_total = int(rng.uniform(600, 3000))
    asleep = max(3600, in_bed - latency - wake_total)
    deep = int(asleep * rng.uniform(0.13, 0.23))
    rem = int(asleep * rng.uniform(0.18, 0.26))
    light = asleep - deep - rem
    end = start + in_bed
    created = end + 120
    data: dict[str, Any] = {
        "total_timeinbed": in_bed,
        "total_sleep_time": asleep,
        "lightsleepduration": light,
        "deepsleepduration": deep,
        "remsleepduration": rem,
        "sleep_efficiency": round(asleep / in_bed, 3),
        "sleep_latency": latency,
        "wakeup_latency": int(rng.uniform(0, 600)),
        "wakeupduration": wake_total,
        "wakeupcount": rng.randint(0, 6),
        "waso": max(0, wake_total - 300),
        "nb_rem_episodes": rng.randint(2, 6),
        "out_of_bed_count": rng.randint(0, 2),
        "hr_average": rng.randint(52, 68),
        "hr_min": rng.randint(42, 51),
        "hr_max": rng.randint(70, 95),
        "rr_average": rng.randint(12, 17),
        "rr_min": rng.randint(9, 11),
        "rr_max": rng.randint(18, 24),
        "snoring": rng.randint(0, 1800),
        "snoringepisodecount": rng.randint(0, 12),
        "sleep_score": rng.randint(45, 98),
        "apnea_hypopnea_index": rng.randint(0, 12),
        "breathing_disturbances_intensity": rng.randint(0, 40),
        "night_events": {"1": [latency], "2": [latency + 60], "3": [asleep], "4": [in_bed]},
    }
    summary: dict[str, Any] = {
        "id": 80_000 + night,
        "timezone": TIMEZONE,
        "model": model,
        "model_id": model_id,
        "startdate": start,
        "enddate": end,
        "date": ymd(end),
        "created": created,
        "modified": created,
        "hash_deviceid": hash_device_id(device_id(seed, model_id)),
        "completed": True,
        "data": data,
    }
    if quirks and night % 5 == 0:  # partial record: a subset of the data, plus an unknown field
        summary["data"] = {k: data[k] for k in ("total_sleep_time", "sleep_score")}
        summary["data"]["new_metric_2027"] = 5
    if not series:
        return summary, None
    segments: list[dict[str, Any]] = []
    t = start + latency
    stage = 1
    while t < end:
        length = int(rng.uniform(600, 2400))
        stop = min(end, t + length)
        state = UNDOCUMENTED_SLEEP_STATE if quirks and not segments else stage
        steps = range(t, stop, 60)
        base_hr = rng.randint(50, 66)
        segments.append(
            {
                "startdate": t,
                "enddate": stop,
                "state": state,
                "model": "Aura Sensor V2" if model_id == 63 else "ScanWatch",
                "model_id": model_id,
                "hr": {str(s): base_hr + rng.randint(-3, 4) for s in steps},
                "rr": {str(s): rng.randint(11, 18) for s in steps},
                "snoring": {str(s): rng.choice((0, 0, 0, 20, 60)) for s in steps},
                "sdnn_1": {str(s): rng.randint(20, 120) for s in steps},
                "rmssd": {str(s): rng.randint(15, 100) for s in steps},
            }
        )
        stage = rng.choices(SLEEP_STATES, weights=(1, 5, 3, 3))[0]
        t = stop
    return summary, {"model": model, "series": segments}


# ------------------------------------------------------------------- activity and workouts


def activity_day(
    seed: int, day_start: int, *, cap: int, quirks: bool = False, index: int = 0
) -> dict[str, Any]:
    """The ``getactivity`` entry of the day starting at ``day_start``; ``modified`` is the day's
    end plus a sync delay, but never after ``cap`` (now)."""
    rng = rng_for(seed, f"activity/{day_start}")
    model_id = 93
    device = device_id(seed, model_id)
    steps = int(clamp(rng.gauss(7500, 3500), 300, 28_000))
    intense = int(rng.uniform(0, 2400))
    moderate = int(rng.uniform(300, 3000))
    record: dict[str, Any] = {
        "date": ymd(day_start),
        "timezone": TIMEZONE,
        "deviceid": device,
        "hash_deviceid": hash_device_id(device),
        "brand": 18,
        "is_tracker": True,
        "modified": min(day_start + DAY + 600, cap),
        "model": "ScanWatch",
        "modelid": model_id,
        "steps": steps,
        "distance": round(steps * rng.uniform(0.65, 0.85), 1),
        "elevation": round(rng.uniform(0, 60), 1),
        "soft": int(rng.uniform(1800, 9000)),
        "moderate": moderate,
        "intense": intense,
        "active": moderate + intense,
        "calories": round(steps * 0.04 + intense * 0.1, 1),
        "totalcalories": round(rng.uniform(1600, 3200), 1),
        "hr_average": rng.randint(62, 88),
        "hr_min": rng.randint(42, 55),
        "hr_max": rng.randint(110, 175),
        "hr_zone_0": rng.randint(3000, 30_000),
        "hr_zone_1": rng.randint(0, 6000),
        "hr_zone_2": rng.randint(0, 2400),
        "hr_zone_3": rng.randint(0, 900),
    }
    if quirks and index % 7 == 3:  # partial record: heart-rate fields and model missing
        for key in ("hr_average", "hr_min", "hr_max", "hr_zone_0", "model"):
            del record[key]
    return record


def workouts_for_day(
    seed: int, day_start: int, *, quirks: bool = False, force: bool = False
) -> list[dict[str, Any]]:
    rng = rng_for(seed, f"workouts/{day_start}")
    if not force and rng.random() > 0.3:
        return []
    start = day_start + int(rng.uniform(6, 20) * 3600)
    length = int(rng.uniform(900, 5400))
    category = UNDOCUMENTED_WORKOUT_CATEGORY if quirks and force else rng.choice(WORKOUT_CATEGORIES)
    device = device_id(seed, 93)
    return [
        {
            "id": 90_000 + day_start // DAY,
            "category": category,
            "timezone": TIMEZONE,
            "model": 93,
            "attrib": 0,
            "startdate": start,
            "enddate": start + length,
            "date": ymd(start),
            "modified": start + length + 300,
            "deviceid": device,
            "data": {
                "calories": rng.randint(80, 900),
                "intensity": rng.randint(10, 80),
                "hr_average": rng.randint(95, 150),
                "hr_min": rng.randint(70, 95),
                "hr_max": rng.randint(150, 185),
                "hr_zone_0": rng.randint(0, 600),
                "hr_zone_1": rng.randint(0, 1200),
                "hr_zone_2": rng.randint(0, 2400),
                "hr_zone_3": rng.randint(0, 900),
                "steps": rng.randint(500, 9000),
                "distance": rng.randint(500, 12_000),
                "pause_duration": rng.randint(0, 300),
            },
        }
    ]


# ---------------------------------------------------------------------------------- intraday


def intraday_day(seed: int, day_start: int) -> dict[int, dict[str, Any]]:
    """Minute samples of one day from a watch that is off the wrist part of the time."""
    rng = rng_for(seed, f"intraday/{day_start}")
    model_id = 93
    device = device_id(seed, model_id)
    samples: dict[int, dict[str, Any]] = {}
    worn = True
    for minute in range(0, 24 * 60):
        if rng.random() < 0.01:
            worn = not worn
        if not worn:
            continue
        ts = day_start + minute * 60
        sample: dict[str, Any] = {
            "deviceid": device,
            "model": "ScanWatch",
            "model_id": model_id,
            "duration": 60,
            "steps": rng.choice((0, 0, 0, 4, 12, 30, 55)),
            "heart_rate": rng.randint(48, 120),
        }
        if minute % 5 == 0:  # sparse optional channels
            sample["spo2_auto"] = rng.randint(93, 100)
            sample["rr"] = rng.randint(11, 20)
        if minute % 15 == 0:
            sample["rmssd"] = round(rng.uniform(15, 90), 1)
            sample["sdnn1"] = round(rng.uniform(20, 110), 1)
            sample["hrv_quality"] = rng.randint(1, 3)
            sample["core_body_temperature"] = round(rng.uniform(36.2, 37.4), 2)
        samples[ts] = sample
    return samples
