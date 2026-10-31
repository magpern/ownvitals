"""Section 8 on explicit R-peak lists: exclusions, HR, SDNN, RMSSD, pNN50 and the minimums.

Synthetic R-peak lists only: invented intervals, no recording.
"""

import numpy as np
import pytest

from ownvitals.ecg import hrv

FS = 1000  # one sample per millisecond, so a list of intervals is a list of R-peaks


def peaks(intervals_ms: list[float], first: float = 100.0) -> np.ndarray:
    return np.concatenate(([first], first + np.cumsum(intervals_ms)))


def test_a_constant_rate_has_hr_and_zero_variability() -> None:
    result = hrv.analyse(peaks([800.0] * 30), FS)
    assert result.hr_bpm == 75.0
    assert result.excluded == ()
    assert result.hrv == hrv.Hrv(0.0, 0.0, 0.0, 30, 29, 0.0)


def test_the_formulas_on_a_known_series() -> None:
    rr = [800.0, 860.0, 790.0, 830.0, 810.0] * 6  # 30 intervals
    result = hrv.analyse(peaks(rr), FS)
    values = np.array(rr)
    steps = np.diff(values)
    assert result.hr_bpm == round(60_000 / values.mean(), 1)
    assert result.hrv is not None
    assert result.hrv.sdnn_ms == round(float(values.std(ddof=1)), 1)
    assert result.hrv.rmssd_ms == round(float(np.sqrt(np.mean(steps**2))), 1)
    assert result.hrv.pnn50_pct == round(100 * float(np.mean(np.abs(steps) > 50)), 1)
    assert (result.hrv.n_nn, result.hrv.n_pairs) == (30, 29)


def test_pnn50_counts_steps_above_50_ms_and_divides_by_pairs() -> None:
    rr = [800.0, 851.0] * 10 + [800.0] * 10  # 20 steps of 51 ms or -51 ms, then 9 of 0
    result = hrv.analyse(peaks(rr), FS)
    assert result.hrv is not None
    assert result.hrv.pnn50_pct == pytest.approx(100 * 20 / 29, abs=0.05)
    exactly_50 = hrv.analyse(peaks([800.0, 850.0] * 15), FS)
    assert exactly_50.hrv is not None
    assert exactly_50.hrv.pnn50_pct == 0.0  # "> 50", not ">= 50"


def test_rmssd_uses_only_adjacent_pairs_of_accepted_intervals() -> None:
    rr = [800.0] * 10 + [400.0] + [800.0, 820.0] * 8  # one premature interval, excluded
    result = hrv.analyse(peaks(rr), FS)
    assert result.excluded == (hrv.Excluded(10, "rr_deviation"),)
    assert result.hrv is not None
    steps = [0.0] * 9 + [20.0, -20.0] * 7 + [20.0]
    assert result.hrv.n_pairs == len(steps)
    assert result.hrv.rmssd_ms == round(float(np.sqrt(np.mean(np.square(steps)))), 1)


def test_intervals_are_deleted_not_interpolated() -> None:
    rr = [800.0] * 12 + [1400.0, 800.0] + [800.0] * 12
    result = hrv.analyse(peaks(rr), FS)
    assert result.hrv is not None
    assert result.hrv.n_nn == 25
    assert result.hrv.sdnn_ms == 0.0


def test_the_range_is_checked_first_and_inclusive() -> None:
    assert hrv.analyse(peaks([272.8] * 20), FS).excluded == ()  # 272.7 ms is the limit
    out = hrv.analyse(peaks([272.6] * 20), FS)
    assert [e.reason for e in out.excluded] == ["rr_out_of_range"] * 20
    assert out.hr_bpm is None  # nothing left after the range check
    assert hrv.analyse(peaks([2000.0] * 20), FS).excluded == ()
    assert hrv.analyse(peaks([2000.5] * 20), FS).excluded[0].reason == "rr_out_of_range"


def test_the_20_percent_rule_is_relative_to_the_last_accepted_interval_and_strict() -> None:
    result = hrv.analyse(peaks([1000.0] * 5 + [1200.0, 1201.0, 800.0, 799.0] + [1000.0] * 10), FS)
    reasons = {e.i: e.reason for e in result.excluded}
    assert 5 not in reasons  # exactly 20 % is accepted (inclusive), the reference moves to 1200
    assert 6 not in reasons  # 1201 is 0.08 % from 1200
    assert 7 in reasons  # 800 is 33 % below 1201
    assert 8 in reasons
    assert 9 not in reasons  # back at 1000 is 16.7 % below 1201: accepted
    assert result.n_rr == 19


def test_the_reference_starts_at_the_median_of_the_first_five_intervals() -> None:
    # two premature intervals among the first five: the median is still the sinus interval
    rr = [1000.0, 600.0, 1400.0, 600.0, 1400.0] + [1000.0] * 20
    result = hrv.analyse(peaks(rr), FS)
    assert [e.i for e in result.excluded] == [1, 2, 3, 4]


def test_hr_counts_the_excluded_ectopic_intervals() -> None:
    rr = [1000.0] * 10 + [600.0, 1400.0] + [1000.0] * 10
    result = hrv.analyse(peaks(rr), FS)
    assert result.hr_bpm == 60.0  # mean 1000 ms over all 22 intervals
    assert [e.i for e in result.excluded] == [10, 11]


def test_the_minimums_for_hrv() -> None:
    assert hrv.analyse(peaks([800.0] * 14), FS).hrv is None  # 14 NN
    assert hrv.analyse(peaks([800.0] * 14), FS).hrv_reasons == ("insufficient_nn",)
    assert hrv.analyse(peaks([800.0] * 15), FS).hrv is not None
    # 15 NN intervals but fewer than 10 adjacent pairs: alternate accepted/excluded
    sparse = [800.0, 400.0] * 15
    result = hrv.analyse(peaks(sparse), FS)
    assert result.hrv is None
    assert "insufficient_nn" in result.hrv_reasons


def test_more_than_20_percent_excluded_gives_too_many_excluded_before_insufficient() -> None:
    rr = [800.0, 400.0] * 4 + [800.0] * 20  # 4 of 28 = 14 %: fine
    assert hrv.analyse(peaks(rr), FS).hrv is not None
    rr = [800.0, 400.0] * 8 + [800.0] * 14  # 8 of 30 excluded: 26.7 %
    result = hrv.analyse(peaks(rr), FS)
    assert result.hrv is None
    assert result.hrv_reasons[0] == "too_many_excluded"
    exactly = hrv.analyse(peaks([800.0, 400.0] * 6 + [800.0] * 18), FS)
    assert exactly.hrv is not None  # 6 of 30 = 20 %, inclusive
    assert exactly.hrv.excluded_fraction == 0.2


def test_no_intervals_is_not_an_error() -> None:
    for rpeaks in (np.zeros(0), np.array([5.0])):
        result = hrv.analyse(rpeaks, FS)
        assert (result.n_rr, result.hr_bpm, result.hrv) == (0, None, None)
        assert result.hrv_reasons == ("insufficient_nn",)


def test_peaks_are_sample_positions_and_the_rate_scales_them() -> None:
    at_300 = hrv.analyse(np.arange(0, 30 * 240, 240, dtype=float), 300)  # 800 ms
    assert at_300.hr_bpm == 75.0


# --- distorted beats (amended 2026-10-31, #40) -------------------------------------------------


def reasons_of(result: hrv.RrAnalysis) -> dict[int, str]:
    return {e.i: e.reason for e in result.excluded}


def test_the_intervals_on_both_sides_of_a_distorted_beat_leave_the_nn_only() -> None:
    rr = [800.0, 830.0, 790.0, 810.0, 800.0, 820.0, 780.0, 800.0] * 4  # 32 intervals
    plain = hrv.analyse(peaks(rr), FS)
    result = hrv.analyse(peaks(rr), FS, distorted=[10])
    assert reasons_of(result) == {9: "distorted_beat", 10: "distorted_beat"}
    assert result.hr_bpm == plain.hr_bpm  # the heart rate keeps them
    assert result.n_rr == plain.n_rr == 32
    assert plain.hrv is not None
    assert result.hrv is not None
    assert result.hrv.n_nn == plain.hrv.n_nn - 2


def test_the_first_and_the_last_beat_have_one_interval_each() -> None:
    rr = [800.0, 810.0] * 16
    result = hrv.analyse(peaks(rr), FS, distorted=[0, 32])
    assert reasons_of(result) == {0: "distorted_beat", 31: "distorted_beat"}


def test_a_distorted_interval_does_not_move_the_reference_of_the_20_percent_rule() -> None:
    """Section 8's rule (b) compares an interval with the last accepted one, and the reference
    starts at the median of the first five that are neither dropped nor distorted."""
    rr = [800.0] * 12
    rr[0] = 1500.0  # touches the distorted beat 1: it must not start the reference
    rr[1] = 1500.0
    result = hrv.analyse(peaks(rr), FS, distorted=[1])
    assert reasons_of(result) == {0: "distorted_beat", 1: "distorted_beat"}  # not rr_deviation
    after = hrv.analyse(peaks([800.0, 800.0, 1500.0] + [800.0] * 9), FS, distorted=[3])
    assert reasons_of(after) == {2: "distorted_beat", 3: "distorted_beat"}
    assert 4 not in reasons_of(after)  # 800 ms after a distorted 1500: the reference stayed 800


def test_out_of_range_comes_before_distorted_and_both_count_toward_the_excluded_fraction() -> None:
    rr = [800.0] * 40
    rr[3] = 200.0  # above 220 bpm
    result = hrv.analyse(peaks(rr), FS, distorted=[3, 20, 21, 22, 23, 24, 30])
    reasons = reasons_of(result)
    assert reasons[3] == "rr_out_of_range"
    assert reasons[2] == "distorted_beat"
    # beat 3: intervals 2 and 3; beats 20-24: intervals 19-24; beat 30: intervals 29 and 30
    assert sorted(reasons) == [2, 3, 19, 20, 21, 22, 23, 24, 29, 30]
    assert len(result.excluded) / 40 > 0.20
    assert result.hrv is None
    assert result.hrv_reasons == ("too_many_excluded",)


def test_an_unconfirmed_interval_is_not_also_a_distorted_one() -> None:
    result = hrv.analyse(peaks([800.0] * 30), FS, unconfirmed=[5], distorted=[5])
    assert reasons_of(result)[5] == "unconfirmed_beat"
    assert reasons_of(result)[4] == "distorted_beat"
