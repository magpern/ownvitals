"""The >= 200-seed sweep (tests file, "Seeds and other tests"). Synthetic strips only.

Skipped unless ``OWNVITALS_ECG_SLOW=1`` (read at collection); run by #40 and by every PR that
bumps a module version::

    OWNVITALS_ECG_SLOW=1 uv run pytest tests/ecg/test_ecg_sweep.py

It asserts the rows of ``tolerances.py`` over 200 seeds per condition and rate, from the same
``calibrate.survey`` that prints the numbers of ``docs/dev/ecg.md``. A seeded case that misses
a tolerance is in the tail of the generator: it is reported there, never re-seeded (tests
file, "HR and HRV"); ``KNOWN_TAIL`` lists the ones that are, with the number allowed.
"""

import os

import calibrate
import measure
import pytest
import scenarios
import tolerances as tol
from scenarios import RATES

from ownvitals.ecg.pipeline import Analysed

SYNTHETIC = True

SLOW_ENV = "OWNVITALS_ECG_SLOW"
pytestmark = pytest.mark.skipif(
    os.environ.get(SLOW_ENV) != "1", reason=f"the >= 200-seed sweep runs only with {SLOW_ENV}=1"
)

SEEDS = 200

# (condition, sampling rate) -> {strip index: highest margin accepted}: the tail strips reported
# in docs/dev/ecg.md, named one by one. Every other strip of the row stays within its tolerance.
KNOWN_TAIL: dict[tuple[str, int], dict[int, float]] = {
    # burst at the start, 52.66 bpm, HR 53.8 against 52.7 true: 1.112 times the 1 bpm tolerance
    ("motion_edge", 500): {18: 1.12},
}

ROWS = sorted(set(tol.GATE_PASS_MIN) | set(tol.POOLED_MIN))


@pytest.fixture(scope="module")
def rows() -> dict[tuple[str, int], calibrate.Row]:
    return {}


def survey(rows: dict[tuple[str, int], calibrate.Row], condition: str, fs: int) -> calibrate.Row:
    if (condition, fs) not in rows:
        rows[(condition, fs)] = calibrate.survey(condition, fs, SEEDS)
    return rows[(condition, fs)]


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", ROWS)
def test_the_sweep_meets_the_gate_and_accuracy_rows(
    rows: dict[tuple[str, int], calibrate.Row], condition: str, fs: int
) -> None:
    row = survey(rows, condition, fs)
    if condition in tol.GATE_PASS_MIN:
        assert row.pass_share >= tol.GATE_PASS_MIN[condition], row
    if condition in tol.POOLED_MIN and row.passed:
        floor = tol.POOLED_MIN[condition]
        assert row.se is not None
        assert row.se >= floor, ("Se", row.tp, row.fn)
        assert row.ppv is not None
        assert row.ppv >= floor, ("+P", row.tp, row.fp)
        assert row.min_se >= tol.strip_se_floor(condition, fs), ("strip Se", row.min_se)
        assert row.min_ppv >= tol.STRIP_FLOOR, ("strip +P", row.min_ppv)
    elif condition in tol.POOLED_MIN:
        assert condition in tol.MAY_FAIL, f"no {condition} strip passed: the statistics are vacuous"


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(tol.GATE_PASS_MAX))
def test_the_sweep_rejects_the_artefacts(condition: str, fs: int) -> None:
    row = calibrate.survey(condition, fs, SEEDS)
    assert row.pass_share <= tol.GATE_PASS_MAX[condition], row


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(set(tol.CLEAN_CONDITIONS) | set(tol.NOISY_CONDITIONS)))
def test_the_sweep_keeps_hr_and_hrv_within_the_tolerances(
    rows: dict[tuple[str, int], calibrate.Row], condition: str, fs: int
) -> None:
    row = survey(rows, condition, fs)
    named = KNOWN_TAIL.get((condition, fs), {})
    if not named:
        for metric, margin in row.margins.items():
            assert margin <= 1.0, (metric, margin, row)
    for case in scenarios.cases(condition, fs, SEEDS) if named else ():
        error = measure.hrv_error(case) if isinstance(case.result, Analysed) else None
        if error is None:
            continue
        worst = max(error.hr_margin, error.sdnn_margin, error.rmssd_margin, error.pnn50_margin)
        assert worst <= named.get(case.index, 1.0), (case.index, worst)
    if tol.group_of(condition) == "clean":
        assert row.max_rr_rms_ms <= tol.RR_ERROR_RMS_MS, row.max_rr_rms_ms
