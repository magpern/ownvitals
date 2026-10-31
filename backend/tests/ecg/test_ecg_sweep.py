"""The >= 200-seed sweep (tests file, "Seeds and other tests"). Synthetic strips only.

Marked ``slow`` and skipped unless ``OWNVITALS_ECG_SLOW=1`` (see ``conftest.py``); run by #40
and by every PR that bumps a module version::

    OWNVITALS_ECG_SLOW=1 uv run pytest tests/ecg/test_ecg_sweep.py

It asserts the rows of ``tolerances.py`` over 200 seeds per condition and rate, from the same
``calibrate.survey`` that prints the numbers of ``docs/dev/ecg.md``. A seeded case that misses
a tolerance is in the tail of the generator: it is reported there, never re-seeded (tests
file, "HR and HRV"); ``KNOWN_TAIL`` lists the ones that are, with the number allowed.
"""

import calibrate
import pytest
import tolerances as tol
from scenarios import RATES

SYNTHETIC = True

SEEDS = 200

# (condition, sampling rate) -> metric -> highest margin accepted for the tail cases reported in
# docs/dev/ecg.md. Empty unless the sweep found a tail miss.
KNOWN_TAIL: dict[tuple[str, int], dict[str, float]] = {}

ROWS = sorted(set(tol.GATE_PASS_MIN) | set(tol.POOLED_MIN))


@pytest.fixture(scope="module")
def rows() -> dict[tuple[str, int], calibrate.Row]:
    return {}


def survey(rows: dict[tuple[str, int], calibrate.Row], condition: str, fs: int) -> calibrate.Row:
    if (condition, fs) not in rows:
        rows[(condition, fs)] = calibrate.survey(condition, fs, SEEDS)
    return rows[(condition, fs)]


@pytest.mark.slow
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
        assert row.min_se >= tol.STRIP_FLOOR, ("strip Se", row.min_se)
        assert row.min_ppv >= tol.STRIP_FLOOR, ("strip +P", row.min_ppv)
    elif condition in tol.POOLED_MIN:
        assert condition in tol.MAY_FAIL, f"no {condition} strip passed: the statistics are vacuous"


@pytest.mark.slow
@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(tol.GATE_PASS_MAX))
def test_the_sweep_rejects_the_artefacts(condition: str, fs: int) -> None:
    row = calibrate.survey(condition, fs, SEEDS)
    assert row.pass_share <= tol.GATE_PASS_MAX[condition], row


@pytest.mark.slow
@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("condition", sorted(set(tol.CLEAN_CONDITIONS) | set(tol.NOISY_CONDITIONS)))
def test_the_sweep_keeps_hr_and_hrv_within_the_tolerances(
    rows: dict[tuple[str, int], calibrate.Row], condition: str, fs: int
) -> None:
    row = survey(rows, condition, fs)
    allowed = KNOWN_TAIL.get((condition, fs), {})
    for metric, margin in row.margins.items():
        assert margin <= max(1.0, allowed.get(metric, 1.0)), (metric, margin, row)
    if tol.group_of(condition) == "clean":
        assert row.max_rr_rms_ms <= tol.RR_ERROR_RMS_MS, row.max_rr_rms_ms
