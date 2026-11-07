"""The OC1-OC5 calculations for the owner ECG report (scale, positions, spectrum, lead
alignment, measure links).

Split out of `validate_rules`, which re-exports every name here. Pure, like the rest of the
package.
"""

import math
from collections.abc import Sequence

import numpy as np

from ownvitals.ecg.filters import condition
from ownvitals.ecg.input import SUPPORTED_RATES
from ownvitals.ecg.rpeaks import detect, matched_flags
from ownvitals.ecg.sqi import welch_density
from ownvitals.ecg.validate_cli import (
    COINCIDE_FRACTION,
    COINCIDE_MS,
    ECG_TYPES,
    LOWPASS_DB,
    MAINS_DB,
    MAINS_SHARE,
    SCALE_MAX_UV,
    SCALE_MIN_UV,
    MeasureLink,
    Recording,
)


def _whole_percent(part: int, whole: int) -> int:
    """Round half up. ``whole`` is positive."""
    return (100 * part + whole // 2) // whole


def _first_lead(recording: Recording) -> tuple[int, ...] | None:
    if not recording.leads:
        return None
    lead = recording.leads[0]
    return lead or None


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
