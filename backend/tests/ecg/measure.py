"""Measure a case against the tests file: the numbers the tests, the sweep and the calibration
script all use, so that they cannot disagree.

SYNTHETIC: computed on generated strips only.
"""

from dataclasses import dataclass

import numpy as np
import tolerances as tol
import truth
from numpy.typing import NDArray
from scenarios import Case

from ownvitals.ecg.pipeline import Analysed


def detected_s(case: Case) -> NDArray[np.float64]:
    """The R-peaks of an accepted span, in seconds."""
    assert isinstance(case.result, Analysed)
    return np.array(case.result.rpeaks) / case.sampling_hz


def span_matching(case: Case) -> truth.Matching:
    return truth.score_span(case.strip, detected_s(case))


@dataclass(frozen=True)
class HrvError:
    """Estimated against true values of one accepted strip, with each error as a share of its
    tolerance (a margin of 1.0 is the edge; the tests require at most 1.0)."""

    hr: float
    sdnn: float
    rmssd: float
    pnn50: float
    rr_rms_ms: float
    hr_margin: float
    sdnn_margin: float
    rmssd_margin: float
    pnn50_margin: float


def span_truth(case: Case) -> truth.Truth:
    det = detected_s(case)
    window = truth.truth_in_span(case.strip, det)
    return truth.truth_values(case.strip.r_times_s[window], case.strip.beat_types[window])


def hrv_error(case: Case) -> HrvError | None:
    """The HR/HRV errors of an accepted strip, or ``None`` when the strip has no ``hrv``."""
    group = tol.group_of(case.condition)
    result = case.result
    assert group is not None
    assert isinstance(result, Analysed)
    if result.rr.hrv is None or result.rr.hr_bpm is None:
        return None
    truth_values = span_truth(case)
    det = detected_s(case)
    window = truth.truth_in_span(case.strip, det)
    matching = truth.match_beats(det, case.strip.r_times_s[window])
    estimate = result.rr.hrv
    sdnn_tol = max(tol.SDNN_MS[group][0], tol.SDNN_MS[group][1] * truth_values.sdnn_ms)
    pair_pct = 100.0 / truth_values.n_pairs
    pnn_tol = (tol.PNN50_PAIRS[group] + truth_values.band) * pair_pct
    hr_err = abs(result.rr.hr_bpm - truth_values.hr_bpm)
    sdnn_err = abs(estimate.sdnn_ms - truth_values.sdnn_ms)
    rmssd_err = abs(estimate.rmssd_ms - truth_values.rmssd_ms)
    pnn_err = abs(estimate.pnn50_pct - truth_values.pnn50_pct)
    return HrvError(
        hr=hr_err,
        sdnn=sdnn_err,
        rmssd=rmssd_err,
        pnn50=pnn_err,
        rr_rms_ms=truth.rr_error_rms_ms(det, case.strip.r_times_s[window], matching),
        hr_margin=hr_err / tol.HR_BPM[group],
        sdnn_margin=sdnn_err / sdnn_tol,
        rmssd_margin=rmssd_err / tol.rmssd_tolerance_ms(case.strip.hr_bpm, group),
        pnn50_margin=pnn_err / pnn_tol,
    )


def hr_borderline(case: Case) -> bool:
    """Whether a true interval of the span lies within 5 ms of the 220 bpm limit, so that the
    HR assertion leaves the strip out (``tolerances.HR_BORDERLINE_MS``)."""
    times = case.strip.r_times_s[truth.truth_in_span(case.strip, detected_s(case))]
    rr = np.diff(times) * 1000.0
    return bool(np.any(np.abs(rr - tol.HR_LIMIT_MS) <= tol.HR_BORDERLINE_MS))


def expects_hrv(case: Case) -> bool:
    """Whether section 8's minimums hold on the true intervals of the span."""
    values = span_truth(case)
    window = truth.truth_in_span(case.strip, detected_s(case))
    types = case.strip.beat_types[window]
    n_all = len(types) - 1
    n_sinus = int(values.sinus_interval.sum())
    excluded = (n_all - n_sinus) / n_all
    return n_sinus >= 15 and values.n_pairs >= 10 and excluded <= 0.20
