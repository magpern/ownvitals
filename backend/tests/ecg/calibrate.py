"""Calibration and sweep numbers for ``docs/dev/ecg.md`` (tests file, "Seeds and other tests").

SYNTHETIC: every strip is generated. Run from ``backend/``::

    uv run python tests/ecg/calibrate.py            # the numbers of the document, 200 seeds
    uv run python tests/ecg/calibrate.py --seeds 20 # the CI size

It prints Markdown tables: the generator's median true RMSSD per heart-rate band (to compare with
the simulation note's 31.0, 13.2, 6.0 and 3.4 ms), the pooled Se/+P, gate pass and worst-case HR
and HRV margins of every condition at both rates, and the gate pass by heart-rate band. The
slow sweep (``test_ecg_sweep.py``) asserts the same rows against ``tolerances.py``, so the
numbers in the document and the tests cannot disagree.

Seeds: strip ``i`` of ``n`` is ``make_strip(i, hr_i, fs)`` with the heart rates evenly spread
over the band of the condition (``scenarios.sinus_hr``); the noise generator of strip ``i`` is
``default_rng([i, crc32(condition), fs])``. The RMSSD medians use ``make_strip(RMSSD_BASE + k,
hr, 300)`` for ``STRIPS_PER_BPM`` strips per whole bpm.
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

for _path in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
    if str(_path) not in sys.path:  # run as a script: the test helpers are not on the path
        sys.path.insert(0, str(_path))

import measure  # noqa: E402
import numpy as np  # noqa: E402
import scenarios  # noqa: E402
import tolerances as tol  # noqa: E402

from ecgsynth import make_strip  # noqa: E402
from ownvitals.ecg.pipeline import Analysed  # noqa: E402

SYNTHETIC = True

RMSSD_BASE = 5000  # first seed of the median-RMSSD strips
STRIPS_PER_BPM = 10
NOTE_MEDIANS = (31.0, 13.2, 6.0, 3.4)  # the simulation note, ms, for the four bands below
BANDS = ((40, 60), (60, 100), (100, 140), (140, 181))
BAND_LABELS = ("40-59", "60-99", "100-139", "140-180")
SWEEP = (
    "clean", "clean_lt50", "white12_ge50", "white12_lt50", "white6", "emg6", "wander_ge50",
    "wander_lt50", "mains", "amplitude_0.3mv", "inverted", "af", "ectopic", "motion_edge",
    "respiratory",
)  # fmt: skip
GATE_ONLY = (
    "motion_half", "dropout_start", "dropout_middle", "dropout_end", "flat_line", "all_zeros",
    "clipping",
)  # fmt: skip


def median_true_rmssd() -> list[tuple[str, float, int]]:
    """(band, median true RMSSD in ms, strips) of the default variability, sinus rhythm."""
    values: list[list[float]] = [[] for _ in BANDS]
    for bpm in range(40, 181):
        for k in range(STRIPS_PER_BPM):
            strip = make_strip(RMSSD_BASE + k, float(bpm), 300)
            rr = np.diff(strip.r_times_s) * 1000.0
            rmssd = float(np.sqrt(np.mean(np.diff(rr) ** 2)))
            for i, (low, high) in enumerate(BANDS):
                if low <= bpm < high:
                    values[i].append(rmssd)
    return [
        (label, float(np.median(v)), len(v)) for label, v in zip(BAND_LABELS, values, strict=True)
    ]


@dataclass
class Row:
    """The numbers of one condition at one sampling rate."""

    condition: str
    sampling_hz: int
    seeds: int
    passed: int = 0
    tp: int = 0
    fn: int = 0
    fp: int = 0
    min_se: float = 1.0
    min_ppv: float = 1.0
    max_rr_rms_ms: float = 0.0
    n_hrv: int = 0
    n_hr: int = 0  # accepted strips whose HR was compared with the truth
    hr_null: int = 0  # accepted strips with HR null (irregular rhythm)
    hr_borderline: int = 0  # strips left out of the HR margin (tolerances.HR_BORDERLINE_MS)
    margins: dict[str, float] = field(default_factory=dict)  # worst error / tolerance, by metric
    rmssd_by_band: list[tuple[float, float]] = field(default_factory=list)  # (worst ms, tol ms)

    @property
    def pass_share(self) -> float:
        return self.passed / self.seeds

    @property
    def se(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def ppv(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None


def survey(condition: str, sampling_hz: int, seeds: int) -> Row:
    """Run ``seeds`` strips of a condition (or set) and collect the numbers of the tests file."""
    row = Row(condition, sampling_hz, seeds)
    row.margins = {"HR": 0.0, "SDNN": 0.0, "RMSSD": 0.0, "pNN50": 0.0}
    row.rmssd_by_band = [(0.0, 0.0)] * len(BANDS)
    group = tol.group_of(condition)
    for case in scenarios.cases(condition, sampling_hz, seeds):
        if not isinstance(case.result, Analysed):
            continue
        row.passed += 1
        row.hr_null += int(case.result.rr.hr_bpm is None)
        matching = measure.span_matching(case)
        row.tp, row.fn, row.fp = row.tp + matching.tp, row.fn + matching.fn, row.fp + matching.fp
        row.min_se, row.min_ppv = min(row.min_se, matching.se), min(row.min_ppv, matching.ppv)
        error = measure.hrv_error(case) if group else None
        if error is None:
            continue
        row.n_hr += 1
        row.n_hrv += int(error.has_hrv)
        row.max_rr_rms_ms = max(row.max_rr_rms_ms, error.rr_rms_ms)
        borderline = measure.hr_borderline(case)
        row.hr_borderline += int(borderline)
        for name, margin in (
            ("HR", 0.0 if borderline else error.hr_margin),
            ("SDNN", error.sdnn_margin),
            ("RMSSD", error.rmssd_margin),
            ("pNN50", error.pnn50_margin),
        ):
            row.margins[name] = max(row.margins[name], margin)
        if not error.has_hrv:
            continue
        assert group is not None
        limit = tol.rmssd_tolerance_ms(case.strip.hr_bpm, group)
        for i, (low, high) in enumerate(BANDS):
            if low <= case.strip.hr_bpm < high and error.rmssd > row.rmssd_by_band[i][0]:
                row.rmssd_by_band[i] = (error.rmssd, limit)
    scenarios.case.cache_clear()  # 200 strips of arrays per condition: do not keep them
    return row


def band_gate(
    condition: str, sampling_hz: int, low_seeds: int, high_seeds: int
) -> tuple[int, int, int, int]:
    """(passed, total) below 50 bpm and from 50 bpm, for a banded sinus condition."""
    low = scenarios.cases(condition, sampling_hz, low_seeds, scenarios.LOW_BAND)
    high = scenarios.cases(condition, sampling_hz, high_seeds, scenarios.HIGH_BAND)
    return (
        sum(isinstance(c.result, Analysed) for c in low),
        len(low),
        sum(isinstance(c.result, Analysed) for c in high),
        len(high),
    )


def percent(value: float | None) -> str:
    return "-" if value is None else f"{100.0 * value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--seeds", type=int, default=200)
    parser.add_argument("--skip-sweep", action="store_true")
    args = parser.parse_args()

    print(
        f"### Median true RMSSD of the generator ({STRIPS_PER_BPM} strips per whole bpm, "
        f"seeds {RMSSD_BASE}..{RMSSD_BASE + STRIPS_PER_BPM - 1}, 300 Hz)\n"
    )
    print("| Band (bpm) | Generator median (ms) | Strips | Note (ms) | Difference |")
    print("|---|---|---|---|---|")
    for (label, median, n), note in zip(median_true_rmssd(), NOTE_MEDIANS, strict=True):
        print(f"| {label} | {median:.2f} | {n} | {note} | {100.0 * (median / note - 1.0):+.1f} % |")
    if args.skip_sweep:
        return

    print(f"\n### Conditions ({args.seeds} seeds per condition and rate)\n")
    print(
        "| Condition | Hz | Gate pass % | Pooled Se % | Pooled +P % | Min strip Se % | "
        "Min strip +P % | HR strips | HR null | HRV strips | Worst HR | Worst SDNN | "
        "Worst RMSSD | Worst pNN50 | Max RR rms (ms) | HR borderline |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    rows = [survey(c, fs, args.seeds) for c in SWEEP for fs in scenarios.RATES]
    for r in rows:
        m = r.margins
        print(
            f"| {r.condition} | {r.sampling_hz} | {percent(r.pass_share)} | {percent(r.se)} | "
            f"{percent(r.ppv)} | {percent(r.min_se if r.passed else None)} | "
            f"{percent(r.min_ppv if r.passed else None)} | {r.n_hr} | {r.hr_null} | "
            f"{r.n_hrv} | {m['HR']:.2f} | "
            f"{m['SDNN']:.2f} | {m['RMSSD']:.2f} | {m['pNN50']:.2f} | {r.max_rr_rms_ms:.1f} | "
            f"{r.hr_borderline} |"
        )

    print("\n### Worst RMSSD error by band, ms (tolerance in brackets)\n")
    print("| Condition | Hz | " + " | ".join(BAND_LABELS) + " |")
    print("|---|---|" + "---|" * len(BANDS))
    for r in rows:
        if r.n_hrv:
            cells = [f"{w:.2f} ({t:.1f})" if t else "-" for w, t in r.rmssd_by_band]
            print(f"| {r.condition} | {r.sampling_hz} | " + " | ".join(cells) + " |")

    print(
        "\n### Gate pass by heart-rate band (below 50 bpm: 40 seeds; from 50 bpm: "
        f"{args.seeds} seeds)\n"
    )
    print("| Condition | Hz | Below 50 bpm | From 50 bpm |")
    print("|---|---|---|---|")
    for condition in scenarios.BANDED:
        for fs in scenarios.RATES:
            lo, n_lo, hi, n_hi = band_gate(condition, fs, 40, args.seeds)
            print(
                f"| {condition} | {fs} | {lo}/{n_lo} ({100.0 * lo / n_lo:.1f} %) | "
                f"{hi}/{n_hi} ({100.0 * hi / n_hi:.1f} %) |"
            )

    print("\n### Conditions the gate must reject (share that passes)\n")
    print("| Condition | Hz | Passes |")
    print("|---|---|---|")
    for condition in GATE_ONLY:
        for fs in scenarios.RATES:
            cases = scenarios.cases(condition, fs, args.seeds)
            passed = sum(isinstance(c.result, Analysed) for c in cases)
            print(f"| {condition} | {fs} | {passed}/{len(cases)} |")


if __name__ == "__main__":
    main()
