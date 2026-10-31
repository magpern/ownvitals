"""Long calibration runs for the autocorrelation-robust trend test and PELT (issue #68).

NOT a test: the name does not start with ``test_``, so pytest and CI do not collect or run it.
It produces the tables in ``docs/dev/analysis-methods.md`` from larger runs than the seeded
tests can afford, and it is how the constants of ``trend.py`` (``AR1_BIAS_CONSTANT``,
``AR1_BIAS_SLOPE``) and ``changepoint.py`` (``PHI_MARGIN``, ``PHI_MAX``, ``MAX_LAG``,
``TRIM_CUTOFF``) were chosen. SYNTHETIC data only (seeded generators).

    cd backend
    uvx uv@0.13.6 run python tests/analysis_methods/calibrate_autocorrelation.py trend
    uvx uv@0.13.6 run python tests/analysis_methods/calibrate_autocorrelation.py changepoint
    uvx uv@0.13.6 run python tests/analysis_methods/calibrate_autocorrelation.py --runs 40000 trend

Options: ``--runs N`` (default: 5000 for trend, 2000 for changepoint; fewer for long series, which
are slow), ``--seed S`` (default 20261031), ``--workers W`` (processes). A cell's seed depends only
on ``--seed`` and the cell, so the output does not depend on the number of workers, and a larger
``--runs`` with the same seed extends the series of a smaller one. Each rate is printed with its
binomial standard error. Output is Markdown; its last line gives the CPU time of the run (summed
over the workers) and the wall time. ``changepoint`` takes minutes. ``--runs 40000 trend`` is the
run behind the trend tables of ``docs/dev/analysis-methods.md``: 8,320 s of CPU time (43 minutes
of wall time on 10 workers of a shared machine), so it is run by hand and never in CI.
"""

import argparse
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from typing import cast

import numpy as np
from methods_builders import ar1, generator, plain_p_value

from ownvitals.analysis.methods.changepoint import (
    estimate_long_run,
    estimate_sigma,
    find_changes,
)
from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.methods.trend import Correction, trend

ALPHA = 0.05
TREND_NS = (8, 10, 12, 15, 20, 30, 60, 100, 200, 365)
TREND_PHIS = (0.0, 0.3, 0.6)
CHANGE_NS = (30, 60, 100, 200, 365)
CHANGE_PHIS = (0.0, 0.3, 0.5, 0.7, 0.8, 0.9)


def cell_seed(seed: int, *parts: object) -> int:
    """A seed that depends on ``seed`` and the cell, not on the order the cells run in."""
    value = seed
    for part in parts:
        for char in repr(part):
            value = (value * 1000003 + ord(char)) % (2**31 - 1)
    return value


def percent(rate: float, runs: int) -> str:
    se = math.sqrt(max(rate * (1.0 - rate), 1e-12) / runs)
    return f"{100 * rate:.1f} ({100 * se:.1f})"


def timed[T](job: tuple[Callable[[T], object], T]) -> tuple[object, float]:
    """Run one cell in a worker and return its result and the CPU seconds it took."""
    cell, args = job
    start = time.process_time()
    result = cell(args)
    return result, time.process_time() - start


class CpuPool:
    """A process pool that adds up the CPU time its cells took (the workers' own, not the wall)."""

    def __init__(self, pool: ProcessPoolExecutor) -> None:
        self.pool = pool
        self.cpu_seconds = 0.0

    def map[T, R](self, cell: Callable[[T], R], jobs: Iterable[T]) -> Iterator[R]:
        for result, seconds in self.pool.map(timed, [(cell, job) for job in jobs]):
            self.cpu_seconds += seconds
            yield cast(R, result)


# --- trend -------------------------------------------------------------------------------------


def trend_series(rng: np.random.Generator, n: int, phi: float, kind: str) -> Series:
    """A series without a trend: ``kind`` is ``reading`` (AR(1) in reading order, irregular days),
    ``regular`` (the same on consecutive days), ``ou`` (a continuous-time AR(1) with the
    coefficient ``phi`` per day, sampled on irregular days) or ``t3`` (AR(1) with heavy tails)."""
    days = np.sort(rng.choice(np.arange(int(n * 1.6)), size=n, replace=False)).astype(float)
    if kind == "regular":
        return Series.from_arrays(
            np.arange(float(n)), ar1(rng, n, phi) if phi else rng.normal(size=n)
        )
    if kind == "ou":
        y = np.empty(n)
        y[0] = rng.normal()
        for i in range(1, n):
            decay = phi ** (days[i] - days[i - 1])
            y[i] = decay * y[i - 1] + math.sqrt(1.0 - decay * decay) * rng.normal()
        return Series.from_arrays(days, y)
    if kind == "t3":
        noise = rng.standard_t(3, size=n + 50)
        values = np.zeros(n + 50)
        for i in range(1, n + 50):
            values[i] = phi * values[i - 1] + noise[i]
        return Series.from_arrays(days, values[50:])
    return Series.from_arrays(days, ar1(rng, n, phi) if phi else rng.normal(size=n))


def trend_cell(args: tuple[int, float, int, int, str]) -> tuple[int, float, int, str, list[float]]:
    n, phi, runs, seed, kind = args
    rng = generator(cell_seed(seed, "trend", n, phi, kind))
    plain = hamed = corrected = 0
    for _ in range(runs):
        series = trend_series(rng, n, phi, kind)
        result = trend(series)
        corrected += result.significant
        plain += plain_p_value(result) < ALPHA
        hamed += trend(series, correction=Correction.HAMED_RAO).significant
    return n, phi, runs, kind, [plain / runs, hamed / runs, corrected / runs]


def power_cell(args: tuple[int, float, int, int]) -> tuple[int, float, int, list[float]]:
    n, drift, runs, seed = args
    rng = generator(cell_seed(seed, "power", n, drift))
    plain = hamed = corrected = 0
    for _ in range(runs):
        null = trend_series(rng, n, 0.0, "reading")
        rise = drift * (null.t - null.t[0]) / (null.t[-1] - null.t[0])
        series = Series.from_arrays(null.t, null.y + rise)
        result = trend(series)
        corrected += result.significant
        plain += plain_p_value(result) < ALPHA
        hamed += trend(series, correction=Correction.HAMED_RAO).significant
    return n, drift, runs, [plain / runs, hamed / runs, corrected / runs]


def runs_for(n: int, runs: int) -> int:
    return runs if n <= 100 else max(runs // 2, 200) if n <= 200 else max(runs // 5, 200)


def run_trend(runs: int, seed: int, pool: CpuPool) -> None:
    print(f"## Trend: share of series with no trend called a trend (alpha {ALPHA}), seed {seed}\n")
    print("Mean (binomial standard error) in percent. Plain Mann-Kendall / Hamed-Rao / AR(1).\n")
    for kind, label, phis, ns in (
        ("reading", "AR(1) in reading order, irregular days", TREND_PHIS, TREND_NS),
        ("regular", "AR(1), consecutive days", (0.3, 0.6), (8, 30, 100)),
        ("ou", "continuous-time AR(1), phi per day, irregular days", (0.3, 0.6), (12, 30, 100)),
        ("t3", "AR(1) with t(3) innovations, irregular days", (0.0, 0.3, 0.6), (12, 30, 100)),
    ):
        jobs = [(n, phi, runs_for(n, runs), seed, kind) for phi in phis for n in ns]
        rows = {(n, phi): (r, v) for n, phi, r, _, v in pool.map(trend_cell, jobs)}
        print(f"### {label}\n")
        print("| phi | n | runs | plain | Hamed-Rao | AR(1) |\n|---|---|---|---|---|---|")
        for phi in phis:
            for n in ns:
                r, v = rows[(n, phi)]
                print(f"| {phi} | {n} | {r} | " + " | ".join(percent(x, r) for x in v) + " |")
        print()
    print("### Power: white noise plus a straight trend of `drift` standard deviations\n")
    print("| n | drift | runs | plain | Hamed-Rao | AR(1) |\n|---|---|---|---|---|---|")
    jobs = [(12, 3.5, runs, seed), (20, 2.5, runs, seed), (30, 2.0, runs, seed)]
    jobs += [(60, 1.6, runs, seed), (200, 0.8, runs, seed), (365, 0.6, runs // 5, seed)]
    for n, drift, r, v in pool.map(power_cell, jobs):
        print(f"| {n} | {drift} | {r} | " + " | ".join(percent(x, r) for x in v) + " |")
    print()


# --- change points -----------------------------------------------------------------------------


def change_cell(args: tuple[int, float, int, int]) -> tuple[int, float, int, list[float]]:
    n, phi, runs, seed = args
    rng = generator(cell_seed(seed, "cp-null", n, phi))
    times = np.arange(float(n))
    spurious = before = 0
    sigmas: list[float] = []
    for _ in range(runs):
        y = ar1(rng, n, phi) if phi else rng.normal(size=n)
        spurious += bool(find_changes(Series(times, y)).change_points)
        before += bool(find_changes(Series(times, y), sigma=estimate_sigma(y)).change_points)
        sigmas.append(estimate_long_run(y).sigma)
    truth = 1.0 / (1.0 - phi)  # the long-run sd of AR(1) with innovations of sd 1
    return n, phi, runs, [spurious / runs, float(np.median(sigmas)) / truth, before / runs]


def planted_cell(args: tuple[int, float, int, int]) -> tuple[int, float, int, list[float]]:
    n, phi, runs, seed = args
    rng = generator(cell_seed(seed, "cp-planted", n, phi))
    times = np.arange(float(n))
    at = n // 2
    one = near = many = 0
    errors: list[int] = []
    for _ in range(runs):
        y = ar1(rng, n, phi) if phi else rng.normal(size=n)
        y[at:] += 2.0 / (1.0 - phi)
        found = find_changes(Series(times, y)).indices
        one += len(found) == 1
        near += any(abs(i - at) <= 5 for i in found)
        many += len(found) > 2
        if len(found) == 1:
            errors.append(abs(found[0] - at))
    return n, phi, runs, [one / runs, near / runs, many / runs, float(np.mean(errors or [0]))]


def run_changepoint(runs: int, seed: int, pool: CpuPool) -> None:
    print(f"## PELT: AR(1) noise, no change, seed {seed}\n")
    print(
        "Share of series with a change point, mean (binomial standard error) in percent: with the"
    )
    print("long-run noise estimate, and with the noise level #38 used (the standard deviation of")
    print("the differences over sqrt(2)). Also the median of the estimated long-run sd over the")
    print("true one (1 = exact).\n")
    print("| phi | n | runs | spurious | as in #38 | median sigma / true |")
    print("|---|---|---|---|---|---|")
    jobs = [(n, phi, runs_for(n, runs), seed) for phi in CHANGE_PHIS for n in CHANGE_NS]
    for n, phi, r, v in pool.map(change_cell, jobs):
        print(f"| {phi} | {n} | {r} | {percent(v[0], r)} | {percent(v[2], r)} | {v[1]:.2f} |")
    print(f"\n## PELT: a shift of 2 long-run standard deviations at n/2, seed {seed}\n")
    print("Exactly one change point / one within 5 points of the shift / more than two, and the")
    print("mean distance of the single change point from the shift (points).\n")
    print("| phi | n | runs | exactly one | within 5 | more than two | mean distance |")
    print("|---|---|---|---|---|---|---|")
    jobs = [(n, phi, runs_for(n, runs) // 2, seed) for phi in CHANGE_PHIS[:3] for n in CHANGE_NS]
    for n, phi, r, v in pool.map(planted_cell, jobs):
        print(
            f"| {phi} | {n} | {r} | {percent(v[0], r)} | {percent(v[1], r)} | "
            f"{percent(v[2], r)} | {v[3]:.2f} |"
        )
    print()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("what", choices=["trend", "changepoint"])
    parser.add_argument("--runs", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20261031)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    options = parser.parse_args(argv)
    runner: Callable[[int, int, CpuPool], None]
    runner, default_runs = (run_trend, 5000) if options.what == "trend" else (run_changepoint, 2000)
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=options.workers) as executor:
        pool = CpuPool(executor)
        runner(options.runs or default_runs, options.seed, pool)
    print(
        f"CPU time {pool.cpu_seconds:.0f} s summed over {options.workers} workers, "
        f"wall time {time.perf_counter() - started:.0f} s."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
