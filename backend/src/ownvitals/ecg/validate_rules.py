"""The OC and V calculations for the owner ECG report.

Imported by `validate_cli.evaluate` at call time so this module can import the
recording types without a cycle. Pure, like the rest of the package.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from ownvitals.ecg.filters import condition
from ownvitals.ecg.input import SUPPORTED_RATES
from ownvitals.ecg.pipeline import analyse, canonical_json, to_estimated
from ownvitals.ecg.rpeaks import detect, matched_flags
from ownvitals.ecg.sqi import welch_density
from ownvitals.ecg.validate_cli import (
    COINCIDE_FRACTION,
    COINCIDE_MS,
    ECG_TYPES,
    HRV_REASONS,
    LOWPASS_DB,
    MAINS_DB,
    MAINS_SHARE,
    NOISE_DETAILS,
    SCALE_MAX_UV,
    SCALE_MIN_UV,
    SQI_REASONS,
    V1_MIN_PERCENT,
    V3_MAX_BPM,
    V3_MIN_PERCENT,
    V4_MAX_WRONG_PERCENT,
    V4_MIN_JUSTIFIED,
    V4_MIN_PASSED,
    V4_MIN_REJECTED,
    MeasureLink,
    OwnerAnswers,
    Recording,
    input_fingerprint,
)


def _whole_percent(part: int, whole: int) -> int:
    """Round half up. ``whole`` is positive."""
    return (100 * part + whole // 2) // whole


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _gate(estimated: Mapping[str, Any] | None) -> str:
    if not isinstance(estimated, Mapping):
        return "none"
    sqi = estimated.get("sqi")
    if not isinstance(sqi, Mapping):
        return "none"
    result = sqi.get("result")
    if result in ("pass", "fail", "not_run"):
        return str(result)
    return "none"


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _sqi_reasons(estimated: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(estimated, Mapping):
        return ()
    sqi = estimated.get("sqi")
    if not isinstance(sqi, Mapping):
        return ()
    return _string_list(sqi.get("reasons"))


def _hrv_reasons(estimated: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(estimated, Mapping):
        return ()
    return _string_list(estimated.get("hrv_reasons"))


def _our_hr(estimated: Mapping[str, Any] | None) -> float | None:
    if not isinstance(estimated, Mapping):
        return None
    value = estimated.get("hr_bpm")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _revision(estimated: Mapping[str, Any] | None) -> int:
    if not isinstance(estimated, Mapping):
        return 0
    revision = _optional_int(estimated.get("input_revision"))
    return 0 if revision is None else revision


def _first_lead(recording: Recording) -> tuple[int, ...] | None:
    if not recording.leads:
        return None
    lead = recording.leads[0]
    return lead or None


def _labels(reasons: Sequence[str], allowed: Sequence[str]) -> set[str]:
    known = set(allowed)
    labels = {reason for reason in reasons if reason in known}
    if any(reason not in known for reason in reasons):
        labels.add("other")
    return labels


def _scale(recordings: Sequence[Recording]) -> str:
    peaks: list[int] = []
    for recording in recordings:
        lead = _first_lead(recording)
        if lead is None:
            continue
        peak = int(np.max(np.abs(np.asarray(lead, dtype=np.int64))))
        peaks.append(peak)
    if not peaks:
        return "not-applicable"
    if all(SCALE_MIN_UV <= peak <= SCALE_MAX_UV for peak in peaks):
        return "pass"
    return "fail"


def _oc1(scale: str, owner: str | None) -> str:
    if scale == "not-applicable" and owner is None:
        return "not-applicable"
    if scale == "fail" or owner == "no":
        return "fail"
    if owner == "yes" and scale == "pass":
        return "pass"
    if owner is None:
        return "pending"
    return "fail"


def _positions(recordings: Sequence[Recording]) -> int:
    return len({rec.wearposition for rec in recordings if rec.wearposition is not None})


def _oc2(positions: int, owner: str | None) -> str:
    if owner is None:
        return "pending"
    if owner == "yes" and positions >= 1:
        return "pass"
    return "fail"


def _spectrum(lead: tuple[int, ...], sampling_hz: int) -> tuple[bool, bool] | None:
    """(low-pass holds, a 50 or 60 Hz line is present), or None when Welch cannot run."""
    if sampling_hz < 2 or len(lead) < 4 * sampling_hz:
        return None
    series = np.asarray(lead, dtype=np.float64) / 1000.0
    try:
        freqs, density = welch_density(series, sampling_hz)
    except (ValueError, IndexError, FloatingPointError):
        return None
    low = (freqs >= 5.0) & (freqs <= 15.0)
    high = freqs > 40.0
    power_low = float(np.sum(density[low]))
    power_high = float(np.sum(density[high]))
    if power_high <= 0.0 and power_low > 0.0:
        lowpass = True
    elif power_low <= 0.0:
        lowpass = False
    else:
        lowpass = 10.0 * math.log10(power_low / power_high) >= LOWPASS_DB
    mains = _mains(freqs, density, 50.0, power_low) or _mains(freqs, density, 60.0, power_low)
    return lowpass, mains


def _mains(freqs: np.ndarray, density: np.ndarray, hz: float, power_low: float) -> bool:
    line = (freqs >= hz - 1.0) & (freqs <= hz + 1.0)
    side = ((freqs >= hz - 10.0) & (freqs <= hz - 3.0)) | (
        (freqs >= hz + 3.0) & (freqs <= hz + 10.0)
    )
    power_line = float(np.sum(density[line]))
    power_side = float(np.sum(density[side]))
    if power_line <= 0.0 or (power_low > 0.0 and power_line < MAINS_SHARE * power_low):
        return False
    if power_side <= 0.0:
        return True
    return 10.0 * math.log10(power_line / power_side) >= MAINS_DB


def _oc3(recordings: Sequence[Recording]) -> tuple[str, str]:
    judged: list[tuple[bool, bool]] = []
    for recording in recordings:
        lead = _first_lead(recording)
        if lead is None or recording.sampling_hz is None:
            continue
        outcome = _spectrum(lead, recording.sampling_hz)
        if outcome is not None:
            judged.append(outcome)
    if not judged:
        return "not-applicable", "not-applicable"
    lowpass = "pass" if all(item[0] for item in judged) else "fail"
    mains = "yes" if any(item[1] for item in judged) else "no"
    return lowpass, mains


def _peaks(lead: tuple[int, ...], sampling_hz: int) -> np.ndarray:
    try:
        conditioned = condition(np.asarray(lead, dtype=np.float64) / 1000.0, float(sampling_hz))
        return detect(conditioned, sampling_hz).primary
    except (ValueError, IndexError, FloatingPointError):
        return np.array([], dtype=np.float64)


def _coincide(left: np.ndarray, right: np.ndarray, sampling_hz: int) -> float:
    if left.size == 0 or right.size == 0:
        return 0.0
    tolerance = COINCIDE_MS * sampling_hz / 1000.0
    forward = float(np.count_nonzero(matched_flags(left, right, tolerance))) / left.size
    backward = float(np.count_nonzero(matched_flags(right, left, tolerance))) / right.size
    return min(forward, backward)


def _oc4(recordings: Sequence[Recording]) -> tuple[str, str]:
    """Pass when every multi-lead recording has equal lengths and >= 95% of R-peaks within 20 ms."""
    ratios: list[float] = []
    multi = False
    for recording in recordings:
        leads = recording.leads
        if leads is None or len(leads) < 2:
            continue
        multi = True
        lengths = {len(lead) for lead in leads}
        rate = recording.sampling_hz
        if (
            len(lengths) != 1
            or 0 in lengths
            or not isinstance(rate, int)
            or rate not in SUPPORTED_RATES
        ):
            ratios.append(0.0)
            continue
        reference = _peaks(leads[0], rate)
        ratios.append(min(_coincide(reference, _peaks(lead, rate), rate) for lead in leads[1:]))
    if not multi:
        return "not-applicable", "not-applicable"
    percent = _whole_percent(int(min(ratios) * 10_000), 10_000) if ratios else 0
    # The displayed percent is rounded; the decision uses the exact ratio.
    passed = bool(ratios) and min(ratios) + 1e-12 >= COINCIDE_FRACTION
    return ("pass" if passed else "fail"), f"{percent}%"


def _matches(recording: Recording, link: MeasureLink) -> bool:
    if link.connection != recording.connection or link.measured_at != recording.recorded_at:
        return False
    if not (link.types & ECG_TYPES):
        return False
    # ADR 0006 A7: the same device. A missing device id is not a match.
    if recording.device_id is None:
        return False
    if recording.device_id in link.device_ids:
        return True
    return (
        not link.device_ids
        and recording.model_id is not None
        and recording.model_id == link.model_id
    )


def _oc5(recordings: Sequence[Recording], links: Sequence[MeasureLink]) -> tuple[str, int]:
    if not recordings:
        return "not-applicable", 0
    missing = sum(
        1 for recording in recordings if not any(_matches(recording, link) for link in links)
    )
    return ("pass" if missing == 0 else "fail"), missing


def _eligible(recording: Recording) -> bool:
    return recording.afib_details not in NOISE_DETAILS


def _v1(recordings: Sequence[Recording]) -> tuple[str, int, str]:
    eligible = [recording for recording in recordings if _eligible(recording)]
    if not eligible:
        return "not-applicable", 0, "not-applicable"
    passed = sum(1 for recording in eligible if _gate(recording.estimated) == "pass")
    percent = _whole_percent(passed, len(eligible))
    return ("pass" if percent >= V1_MIN_PERCENT else "fail"), len(eligible), f"{percent}%"


def _band(heart_rate: int | None) -> str:
    if heart_rate is None:
        return "unknown"
    if heart_rate < 50:
        return "under-50"
    return "from-50"


def _agreement(recordings: Sequence[Recording]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for recording in recordings:
        side = "withings-noise" if recording.afib_details in NOISE_DETAILS else "withings-clean"
        gate = _gate(recording.estimated)
        our = "our-pass" if gate == "pass" else "our-fail" if gate == "fail" else "our-none"
        counts[(side, our)] += 1
    return counts


def _hrv_counts(recordings: Sequence[Recording]) -> tuple[int, Counter[str]]:
    passed = [recording for recording in recordings if _gate(recording.estimated) == "pass"]
    counts: Counter[str] = Counter()
    for recording in passed:
        for label in _labels(_hrv_reasons(recording.estimated), HRV_REASONS):
            counts[label] += 1
    return len(passed), counts


def _failure_counts(recordings: Sequence[Recording]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for recording in recordings:
        if _gate(recording.estimated) != "fail":
            continue
        band = _band(recording.heart_rate)
        labels = _labels(_sqi_reasons(recording.estimated), SQI_REASONS)
        if not labels:
            labels = {"none"}
        for label in labels:
            counts[(band, label)] += 1
    return counts


def _v3(recordings: Sequence[Recording]) -> tuple[str, int, str, str, str]:
    diffs: list[float] = []
    for recording in recordings:
        if _gate(recording.estimated) != "pass" or recording.heart_rate is None:
            continue
        if "irregular_rhythm" in _hrv_reasons(recording.estimated):
            continue
        ours = _our_hr(recording.estimated)
        if ours is None:
            continue
        diffs.append(ours - float(recording.heart_rate))
    if not diffs:
        return "not-applicable", 0, "not-applicable", "none", "none"
    within = sum(1 for diff in diffs if abs(diff) <= V3_MAX_BPM)
    percent = _whole_percent(within, len(diffs))
    sign, limits = _bland(diffs)
    status = "pass" if percent >= V3_MIN_PERCENT else "fail"
    return status, len(diffs), f"{percent}%", sign, limits


def _bland(diffs: Sequence[float]) -> tuple[str, str]:
    count = len(diffs)
    bias = sum(diffs) / count
    sign = "positive" if bias > 0.05 else "negative" if bias < -0.05 else "zero"
    if count < 2:
        return sign, "none"
    variance = sum((diff - bias) ** 2 for diff in diffs) / (count - 1)
    spread = math.sqrt(variance)
    low, high = bias - 1.96 * spread, bias + 1.96 * spread
    return sign, "yes" if abs(low) <= V3_MAX_BPM and abs(high) <= V3_MAX_BPM else "no"


def _v4(recordings: Sequence[Recording], answers: OwnerAnswers) -> tuple[str, int, int, str, str]:
    passed = sum(1 for recording in recordings if _gate(recording.estimated) == "pass")
    rejected = sum(1 for recording in recordings if _gate(recording.estimated) == "fail")
    wrong_beats = answers.v4_wrong_beats
    inspected = answers.v4_inspected_beats
    justified_n = answers.v4_justified
    if wrong_beats is None or inspected is None or justified_n is None:
        return "pending", passed, rejected, "pending", "pending"
    if inspected < 1 or wrong_beats > inspected:
        return "fail", passed, rejected, "not-applicable", str(justified_n)
    wrong_percent = _whole_percent(wrong_beats, inspected)
    enough = passed >= V4_MIN_PASSED and rejected >= V4_MIN_REJECTED
    beats_ok = wrong_percent <= V4_MAX_WRONG_PERCENT
    justified_ok = justified_n >= V4_MIN_JUSTIFIED
    status = "pass" if enough and beats_ok and justified_ok else "fail"
    return status, passed, rejected, f"{wrong_percent}%", str(justified_n)


def _recomputed(recording: Recording) -> dict[str, Any]:
    leads = None if not recording.leads else recording.leads
    digest = recording.payload_hash if leads is not None else None
    result = analyse(leads, recording.sampling_hz)
    return to_estimated(
        result,
        input_fingerprint=input_fingerprint(recording.signalid, digest, recording.sampling_hz),
        input_revision=_revision(recording.estimated),
    )


def _v5(recordings: Sequence[Recording]) -> tuple[str, int, int]:
    compared = 0
    mismatch = 0
    for recording in recordings:
        if recording.estimated is None and not recording.leads:
            continue
        compared += 1
        stored = recording.estimated if isinstance(recording.estimated, dict) else None
        if stored is None or canonical_json(stored) != canonical_json(_recomputed(recording)):
            mismatch += 1
    if compared == 0:
        return "not-applicable", 0, 0
    return ("pass" if mismatch == 0 else "fail"), compared, mismatch
