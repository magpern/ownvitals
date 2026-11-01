"""HR and HRV against the generator's ground truth (tests file, "HR and HRV"). Synthetic strips.

The truth is ``truth.py``, an independent restatement of section 8; the tolerances are in
``tolerances.py``. Each assertion is one row of the tests file.
"""

import measure
import numpy as np
import pytest
import scenarios
import tolerances as tol
import truth
from scenarios import RATES

from ownvitals.ecg.pipeline import Analysed

HRV_CONDITIONS = sorted(tol.CLEAN_CONDITIONS + tol.NOISY_CONDITIONS)


def accepted(condition: str, fs: int) -> list[scenarios.Case]:
    return [c for c in scenarios.cases(condition, fs) if isinstance(c.result, Analysed)]


# Where section 8's minimums hold on the *true* intervals, hrv is computed. The noisy conditions
# are the exception: a strip whose beats are distorted loses the intervals that touch them (the
# `distorted_beat` rule), and may then fall under section 8's minimums.
STRICT_CONDITIONS = sorted((*tol.CLEAN_CONDITIONS, "ectopic"))


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", STRICT_CONDITIONS)
def test_hrv_is_computed_where_the_truth_allows_it(condition: str, fs: int) -> None:
    strips = accepted(condition, fs)
    assert strips
    for case in strips:
        assert isinstance(case.result, Analysed)
        if measure.expects_hrv(case):
            assert case.result.rr.hrv is not None, (case.index, case.result.rr.hrv_reasons)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", HRV_CONDITIONS)
def test_hrv_is_withheld_only_when_section_8s_minimums_fail_on_the_pipelines_own_exclusions(
    condition: str, fs: int
) -> None:
    for case in accepted(condition, fs):
        assert isinstance(case.result, Analysed)
        rr = case.result.rr
        if rr.hrv is not None:
            assert rr.hrv_reasons == ()
            continue
        assert rr.hrv_reasons, (case.index, "hrv is null without a reason")
        n_nn = rr.n_rr - len(rr.excluded)
        if "too_many_excluded" in rr.hrv_reasons:
            assert len(rr.excluded) / rr.n_rr > 0.20, (case.index, rr.hrv_reasons)
        else:
            assert rr.hrv_reasons == ("insufficient_nn",)
            assert n_nn < 15 or rr.hrv_reasons, (case.index, n_nn)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", HRV_CONDITIONS)
def test_hr_and_hrv_match_the_ground_truth_within_the_tolerances(condition: str, fs: int) -> None:
    for case in accepted(condition, fs):
        error = measure.hrv_error(case)
        if error is None:
            continue
        where = (case.index, round(case.strip.hr_bpm))
        if not measure.hr_borderline(case):  # see tolerances.HR_BORDERLINE_MS
            assert error.hr_margin <= 1.0, ("HR", where, error)
        assert error.sdnn_margin <= 1.0, ("SDNN", where, error)
        assert error.rmssd_margin <= 1.0, ("RMSSD", where, error)
        assert error.pnn50_margin <= 1.0, ("pNN50", where, error)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", ["clean", "amplitude_0.3mv", "inverted", "respiratory"])
def test_the_rr_error_of_clean_strips_is_at_most_2_ms_rms(condition: str, fs: int) -> None:
    for case in accepted(condition, fs):
        error = measure.hrv_error(case)
        assert error is not None
        assert error.rr_rms_ms <= tol.RR_ERROR_RMS_MS, (case.index, error.rr_rms_ms)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", ["clean", "amplitude_0.3mv", "inverted"])
def test_no_sinus_interval_is_excluded_under_the_default_variability(
    condition: str, fs: int
) -> None:
    for case in accepted(condition, fs):
        assert isinstance(case.result, Analysed)
        assert case.result.rr.excluded == ()


# --- ectopic beats, respiratory modulation, AF-like strips ---------------------------------------


@pytest.mark.parametrize("fs", RATES)
def test_every_premature_interval_and_pause_is_excluded_and_no_sinus_interval_is(fs: int) -> None:
    compared = 0
    for case in accepted("ectopic", fs):
        assert isinstance(case.result, Analysed)
        det = measure.detected_s(case)
        window = truth.truth_in_span(case.strip, det)
        times = case.strip.r_times_s[window]
        types = case.strip.beat_types[window]
        matching = truth.match_beats(det, times)
        by_detection = {j: i for i, j in matching.pairs}
        excluded = {e.i for e in case.result.rr.excluded}
        for k in range(len(det) - 1):
            if k not in by_detection or k + 1 not in by_detection:
                continue
            i, i_next = by_detection[k], by_detection[k + 1]
            if i_next != i + 1:
                continue  # a beat was missed in between: not a property of the exclusion rules
            ectopic = "V" in (types[i], types[i_next])
            assert (k in excluded) == ectopic, (case.index, k, types[i : i_next + 1])
            compared += 1
    assert compared > 800


@pytest.mark.parametrize("fs", RATES)
def test_ectopic_strips_have_ectopic_beats_anywhere_including_the_first_intervals(fs: int) -> None:
    early = [
        c
        for c in scenarios.cases("ectopic", fs)
        if any(
            t < 2.5
            for t, kind in zip(c.strip.r_times_s, c.strip.beat_types, strict=True)
            if kind == "V"
        )
    ]
    assert early  # the seeds do put an ectopic beat in the first five intervals


@pytest.mark.parametrize("fs", RATES)
def test_respiratory_modulation_at_55_bpm_and_above_excludes_at_most_the_first(fs: int) -> None:
    strips = accepted("respiratory", fs)
    assert len(strips) == scenarios.N_SEEDS
    for case in strips:
        assert isinstance(case.result, Analysed)
        assert all(e.i == 0 for e in case.result.rr.excluded), case.result.rr.excluded


@pytest.mark.parametrize("fs", RATES)
def test_respiratory_modulation_at_40_and_45_bpm_hits_the_20_percent_rule(fs: int) -> None:
    """Section 8's limit: the intervals more than 20 % from the last accepted one are excluded
    (no decision contradicts the rule on the true intervals; one within 0.5 % of the limit is
    open, see ``truth.exclusion_violations``), and hrv is computed from the rest or null with
    its reasons."""
    some_excluded = 0
    for case in scenarios.cases("respiratory_slow", fs):
        if not isinstance(case.result, Analysed):
            continue
        det = measure.detected_s(case)
        window = truth.truth_in_span(case.strip, det)
        times = case.strip.r_times_s[window]
        matching = truth.match_beats(det, times)
        if (matching.fn, matching.fp) != (0, 0):
            continue
        got = [e.i for e in case.result.rr.excluded]
        assert truth.exclusion_violations(np.diff(times) * 1000.0, set(got)) == [], case.index
        some_excluded += bool(got)
        fraction = len(got) / case.result.rr.n_rr
        if case.result.rr.hrv is not None:
            assert fraction <= 0.20
        elif fraction > 0.20:
            assert "too_many_excluded" in case.result.rr.hrv_reasons
    assert some_excluded  # the limit is really reached by these strips


@pytest.mark.parametrize("fs", RATES)
def test_af_like_strips_have_no_hr_or_hrv_because_too_many_intervals_are_excluded(fs: int) -> None:
    strips = accepted("af", fs)
    assert strips
    for case in strips:
        assert isinstance(case.result, Analysed)
        assert case.result.rr.hrv is None
        assert "too_many_excluded" in case.result.rr.hrv_reasons
        # an irregular rhythm has no checked HR: null, with its reason (decision on #114)
        assert case.result.rr.hr_bpm is None
        assert case.result.rr.hrv_reasons[-1] == "irregular_rhythm"


@pytest.mark.parametrize("fs", RATES)
def test_hr_is_null_exactly_when_the_rhythm_is_irregular_in_every_condition(fs: int) -> None:
    """HR is stored whenever the gate passes (section 8), except on an irregular rhythm."""
    with_hr = without_hr = 0
    for condition in scenarios.BUILDERS:
        for case in accepted(condition, fs):
            assert isinstance(case.result, Analysed)
            rr = case.result.rr
            assert (rr.hr_bpm is None) == ("irregular_rhythm" in rr.hrv_reasons), (
                condition,
                case.index,
            )
            with_hr += rr.hr_bpm is not None
            without_hr += rr.hr_bpm is None
    assert with_hr
    assert without_hr  # the AF-like strips


@pytest.mark.parametrize("fs", RATES)
def test_more_than_20_percent_excluded_means_no_hrv_for_every_condition(fs: int) -> None:
    checked = 0
    for condition in scenarios.BUILDERS:
        for case in accepted(condition, fs):
            assert isinstance(case.result, Analysed)
            rr = case.result.rr
            if rr.n_rr and len(rr.excluded) / rr.n_rr > 0.20:
                assert rr.hrv is None
                assert rr.hrv_reasons[0] == "too_many_excluded"
                checked += 1
    assert checked  # the AF-like strips at least
