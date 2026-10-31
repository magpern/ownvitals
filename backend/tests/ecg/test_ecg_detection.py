"""Beat matching and the gate (tests file, "Beat matching and the gate"). Synthetic strips only.

Se and +P are pooled per condition and sampling rate over the beats of accepted spans (ANSI/AAMI
EC57 gross statistics), with a floor on every accepted strip. The thresholds are in
``tolerances.py``.
"""

import measure
import pytest
import scenarios
import tolerances as tol
from scenarios import RATES

from ownvitals.ecg.pipeline import Analysed, QualityRejected


def accepted(condition: str, fs: int) -> list[scenarios.Case]:
    return [c for c in scenarios.cases(condition, fs) if isinstance(c.result, Analysed)]


def pass_share(condition: str, fs: int) -> float:
    cases = scenarios.cases(condition, fs)
    return sum(isinstance(c.result, Analysed) for c in cases) / len(cases)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(tol.GATE_PASS_MIN))
def test_the_gate_passes_enough_strips(condition: str, fs: int) -> None:
    cases = scenarios.cases(condition, fs)
    share = pass_share(condition, fs)
    assert share >= tol.GATE_PASS_MIN[condition], [
        (c.index, round(c.strip.hr_bpm), getattr(c.result, "reasons", None))
        for c in cases
        if isinstance(c.result, QualityRejected)
    ]


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(tol.GATE_PASS_MAX))
def test_the_gate_rejects_the_artefacts(condition: str, fs: int) -> None:
    assert pass_share(condition, fs) <= tol.GATE_PASS_MAX[condition]


@pytest.mark.parametrize("fs", RATES)
def test_white_noise_at_6_db_may_pass_or_fail_but_never_crashes(fs: int) -> None:
    for condition in ("white6", "emg6"):
        assert all(
            isinstance(c.result, Analysed | QualityRejected) for c in scenarios.cases(condition, fs)
        )


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(tol.POOLED_MIN))
def test_pooled_sensitivity_and_predictivity(condition: str, fs: int) -> None:
    strips = accepted(condition, fs)
    if condition in tol.MAY_FAIL and not strips:
        pytest.skip("the gate may reject every strip of this set")
    assert strips, f"no {condition} strip passed at {fs} Hz: the statistics would be vacuous"
    tp = fn = fp = 0
    for case in strips:
        matching = measure.span_matching(case)
        tp, fn, fp = tp + matching.tp, fn + matching.fn, fp + matching.fp
    floor = tol.POOLED_MIN[condition]
    assert tp / (tp + fn) >= floor, ("Se", tp, fn)
    assert tp / (tp + fp) >= floor, ("+P", tp, fp)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(tol.POOLED_MIN))
def test_every_accepted_strip_keeps_95_percent(condition: str, fs: int) -> None:
    for case in accepted(condition, fs):
        matching = measure.span_matching(case)
        assert matching.se >= tol.STRIP_FLOOR, (case.index, round(case.strip.hr_bpm), matching)
        assert matching.ppv >= tol.STRIP_FLOOR, (case.index, round(case.strip.hr_bpm), matching)


@pytest.mark.parametrize("fs", RATES)
def test_clean_strips_are_matched_perfectly_from_40_to_180_bpm(fs: int) -> None:
    for case in scenarios.cases("clean", fs):
        assert isinstance(case.result, Analysed), (case.index, case.strip.hr_bpm)
        matching = measure.span_matching(case)
        assert (matching.fn, matching.fp) == (0, 0), (case.index, case.strip.hr_bpm)


# --- what each kind of artefact is rejected for -------------------------------------------------


def reasons(case: scenarios.Case) -> tuple[str, ...]:
    assert isinstance(case.result, QualityRejected)
    return case.result.reasons


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", ["dropout_start", "dropout_middle", "dropout_end"])
def test_a_dropout_is_rejected_as_a_flat_stretch_without_a_20_s_span(
    condition: str, fs: int
) -> None:
    for case in scenarios.cases(condition, fs):
        assert {"too_short", "flat_line"} <= set(reasons(case))


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", ["flat_line", "all_zeros"])
def test_a_flat_line_is_rejected_as_flat(condition: str, fs: int) -> None:
    for case in scenarios.cases(condition, fs):
        assert {"too_short", "flat_line", "beat_gap"} <= set(reasons(case))


@pytest.mark.parametrize("fs", RATES)
def test_clipping_on_every_r_peak_is_rejected_as_clipping(fs: int) -> None:
    for case in scenarios.cases("clipping", fs):
        assert {"too_short", "clipping"} <= set(reasons(case))


@pytest.mark.parametrize("fs", RATES)
def test_heavy_motion_is_rejected_for_the_beats_it_hides(fs: int) -> None:
    rejected = [
        c for c in scenarios.cases("motion_half", fs) if isinstance(c.result, QualityRejected)
    ]
    assert rejected
    for case in rejected:
        assert {"detector_disagreement", "low_template_correlation"} & set(reasons(case))


@pytest.mark.parametrize("fs", RATES)
def test_an_irregular_rhythm_is_not_rejected_for_being_irregular(fs: int) -> None:
    """No index uses RR regularity: the AF-like and ectopic strips that fail do not fail on it."""
    for condition in ("af", "ectopic"):
        for case in scenarios.cases(condition, fs):
            if isinstance(case.result, QualityRejected):
                assert "implausible_rate" not in reasons(case)
                assert "beat_gap" not in reasons(case)
