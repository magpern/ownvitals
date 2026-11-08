"""The OC and V calculations for the owner ECG report.

Imported by `validate_cli.evaluate` at call time so this module can import the
recording types without a cycle. Pure, like the rest of the package.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from ownvitals.ecg.pipeline import analyse, canonical_json, to_estimated
from ownvitals.ecg.validate_cli import (
    HRV_REASONS,
    NOISE_DETAILS,
    SQI_REASONS,
    V1_MIN_PERCENT,
    V3_MAX_BPM,
    V3_MIN_PERCENT,
    V4_MAX_WRONG_PERCENT,
    V4_MIN_JUSTIFIED,
    V4_MIN_PASSED,
    V4_MIN_REJECTED,
    OwnerAnswers,
    Recording,
    input_fingerprint,
)
from ownvitals.ecg.validate_rules_oc import (
    _first_lead as _first_lead,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _matches as _matches,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _oc1 as _oc1,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _oc2 as _oc2,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _oc3 as _oc3,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _oc4 as _oc4,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _oc5 as _oc5,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _positions as _positions,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _scale as _scale,  # pyright: ignore[reportPrivateUsage]
)
from ownvitals.ecg.validate_rules_oc import (
    _whole_percent as _whole_percent,  # pyright: ignore[reportPrivateUsage]
)


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


def _labels(reasons: Sequence[str], allowed: Sequence[str]) -> set[str]:
    known = set(allowed)
    labels = {reason for reason in reasons if reason in known}
    if any(reason not in known for reason in reasons):
        labels.add("other")
    return labels


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
