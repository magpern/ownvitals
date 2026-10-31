# Analysis methods: baseline, EWMA, trend, change points

`backend/src/ownvitals/analysis/methods/` holds the statistical methods of PLAN §7.1 step 5 for
irregular, autocorrelated series, pulled forward as a pure library next to the analysis core
(see [analysis.md](analysis.md) for quality gates, daily values and the minimum-data policy).
Wiring into `metric_daily`, findings and jobs comes later in M4.

**Pure**: numpy and scipy only; no database, web, Withings, job, egress or `httpx` import, no
I/O, no clock, no randomness. `tests/test_analysis_purity.py` covers the whole of
`ownvitals.analysis`, so it covers this package. Every function is deterministic and does not
depend on the order its input was passed in.

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/analysis_methods tests/test_analysis_purity.py
```

No database is needed. The simulation tests take about a minute.

## Modules

| Module | What |
|---|---|
| `series.py` | `Series` (times in days, `float64` values), `InputSummary` (n and coverage), `mad()`, `MAD_SCALE` |
| `baseline.py` | `rolling_baseline()`: median, MAD and typical range over time windows |
| `ewma.py` | `ewma()`: exponential average weighted by the time gap |
| `trend.py` | `trend()`: Theil-Sen slope, Mann-Kendall test, Hamed-Rao correction |
| `changepoint.py` | `find_changes()` (PELT) and `detect_changes()` (PELT behind the minimum-data policy) |

The flow, for one metric of one user:

```python
daily = daily_values(result.usable)                       # analysis core
series = Series.from_daily(daily, window_days=90)         # Decimal -> float happens here
base = rolling_baseline(series, window_days=90, window=Window.PRECEDING)
smooth = ewma(series, halflife_days=7)
verdict = trend(series)                                   # after the minimum-data check
found = detect_changes(daily, metric_key="weight", window_end=today, window_days=90)
```

## Conventions

**`Series`.** Times are floats in days (`Series.from_daily`: days since 1970-01-01 of the local
day; `Series.from_instants`: days since 1970-01-01T00:00Z of a timezone-aware instant). Values are
`float64`: the exact `Decimal` of the quality and daily steps becomes a float once, here, as
`analysis.md` says. Input must be finite. `Series(t, y)` requires sorted times;
`Series.from_arrays` sorts by `(t, y)`, so the result never depends on arrival order. The arrays
are copied and read-only. Tied times are allowed in a `Series`; each method says what it does
with them.

**Results carry their inputs.** Every result has `inputs: InputSummary` (`n` points, `n_days`
distinct days, `window_days`, `coverage = n_days / window_days`, first and last time) and the
`algo_version` of its module (`baseline/1`, `ewma/1`, `trend/1`, `changepoint/1`; PLAN §5). The
window is the one given to `Series(..., window_days=...)` (the period the finding is about, e.g.
90) or, if none, the span from the first to the last day with data. **Bump a module
`ALGO_VERSION` whenever its behaviour changes**, in the same change as the tests.
`as_dict()` gives a JSON-safe form (NaN becomes `null`).

**Insufficient data.** The methods compute what they are given; whether there is enough is the
job of the policy (`ownvitals.analysis.policy`). Only `detect_changes` applies it itself, because
the spec says PELT runs only on series that pass it (PLAN §7.1 step 5). Elsewhere the caller
checks first, as in `analysis.md`. `trend` refuses fewer than 3 points.

## Robust baseline (`baseline.py`)

Rolling median and MAD over **time windows**. For each query time `q` the window holds the points
whose time lies in an interval of `window_days` around `q`, however many that is. Alignments
(closed intervals unless noted): `TRAILING` `[q - W, q]`; `PRECEDING` `[q - W, q)`, the history
without the point at `q`, the one to compare a new value with; `CENTERED` `[q - W/2, q + W/2]`.

- `median` and `mad` (raw, unscaled) of the values in the window.
- `scale = max(1.4826 * mad, min_scale)`: the standard-deviation equivalent of the MAD for normal
  data (Rousseeuw and Croux 1993). `min_scale` stops a window of identical values (a scale with
  a coarse resolution) from having a zero-width range.
- typical range `median +- k * scale`, `k = 2` by default (about the central 95 % of a normal
  distribution).
- a window with fewer than `min_points` (default 3) points is `valid = False` with NaN
  statistics. Nothing is interpolated across a gap. `n_window` says how many points each window
  held.
- `at=` evaluates at any times, not only the times of the series.

Boundaries are exact for whole days; for fractional days they are subject to float rounding.

## Gap-weighted EWMA (`ewma.py`)

`alpha_i = 1 - 2^(-gap_i / halflife)`, `E_0 = y_0`, `E_i = alpha_i y_i + (1 - alpha_i) E_(i-1)`.
The weight of a reading depends on the time since the previous one, not on how many readings
there are: one half-life after the last reading it weighs one half, after a long gap nearly one.
This is the exponential moving average of an unevenly spaced series with next-point
interpolation (Eckner 2019, section 3.1). Because `(1 - alpha(d1))(1 - alpha(d2)) = 1 -
alpha(d1 + d2)`, adding points that repeat the value of the next point changes nothing (tested).

- the first reading initialises the average, so treat the first few half-lives as a warm-up;
- readings at the same time are merged into their mean first (their order cannot matter), and
  each gets the value of the merged point;
- `halflife_days` is a parameter; this module does not choose smoothing for any metric.

## Theil-Sen and modified Mann-Kendall (`trend.py`)

- **Mann-Kendall** (Mann 1945, Kendall 1975): `S = sum sign(y_j - y_i)`, tie-corrected
  `Var(S)`, continuity-corrected `Z`, two-sided `p`. It uses only the order of the values.
  Tied times are refused (aggregate to one value per day first); a constant series gives
  `Z = 0, p = 1`.
- **Hamed-Rao** (1998): detrend with the Theil-Sen slope, rank the residuals, take the
  autocorrelation of the ranks at the lags significant at 95 % (Anderson limits) and multiply
  `Var(S)` by `n/n* = 1 + 2/(n(n-1)(n-2)) * sum (n-k)(n-k-1)(n-k-2) rho_k`.
- **Theil-Sen** (Theil 1950, Sen 1968): the median of all pairwise slopes `(y_j - y_i)/(t_j - t_i)`
  with the real times, so the slope is in value units **per day**. The confidence interval of Sen
  uses the modified variance (ranks `(N - C)/2` and `(N + C)/2 + 1`, interpolated, as in Gilbert
  1987 and Helsel and Hirsch 2002). The intercept is the level at `t_ref`, the first time.
- `correction=Correction.NONE` gives the plain test; the result always says which was used and
  carries `n_over_n_star_raw` (as published) and `n_over_n_star` (as used).

Decisions the sources leave open:

- **`n/n*` is never below 1.** The published factor drops below 1 for negative autocorrelation,
  which would make the test claim more than the plain one; we only inflate.
- **The autocorrelation lag counts points in time order.** On a series with gaps a lag is "k
  readings later", not "k days later". The residuals themselves use the real times.
- At most `MAX_POINTS = 3000` points (the `n(n-1)/2` slopes are held in memory); trends are
  computed over windows of weeks to a year.

### What the correction achieves (measured, seeded, 1000 series each)

Series with no trend at all, tested at alpha = 0.05 (the tests pin these runs):

| Series | Plain MK | Hamed-Rao |
|---|---|---|
| white noise, n = 60, irregular times | 5.3 % | 5.0 % |
| AR(1), phi = 0.3, n = 100 | 13.1 % | 8.7 % |
| AR(1), phi = 0.6, n = 100 | 29.2 % | 13.5 % |

White noise is held at the stated level (at alpha = 0.01: 1.0 % and 0.9 %). On autocorrelated
series the correction removes about half of the excess, but **it does not restore alpha**: as
published it uses only the significant lags, so it under-corrects, more the stronger the
autocorrelation. A finding from a strongly autocorrelated metric (day-to-day weight) is
therefore less conservative than its p-value says. The `confidence` the wiring step derives
from this must not treat `p < alpha` as a guarantee at alpha. Options if that matters: a
stronger correction (effective sample size from a fitted AR model, prewhitening), a stricter
alpha, or a block bootstrap; none is in this issue.

The acceptance criterion "white noise: no trend in at least 95 % of seeded runs at the stated
alpha" is tested two ways: at alpha = 0.01 the literal 95 % holds; at alpha = 0.05 the rate must
not exceed alpha plus three binomial standard errors (a seeded sample of 1000 cannot promise
95.0 % when the own rate of the test is 5.0 %).

## PELT change points (`changepoint.py`)

PELT (Killick, Fearnhead and Eckley 2012) finds the segmentation of the points in time order
that minimises `sum cost(segment) + penalty * (number of change points)`, pruning the candidates
that can never be optimal again (their Theorem 3.1, `K = 0`). It is **exact**: the tests compare
it with the unpruned O(n^2) optimal-partitioning recursion (Jackson et al. 2005) on random series
with planted shifts and minimum segment lengths 1 to 5, and it returns the same optimum.

The model and its parameters (ours; the paper leaves them open):

- **Cost**: Gaussian mean shift, `sum (y - mean)^2 / sigma^2` per segment (minus twice the
  log-likelihood with a common variance). The cost ignores the spacing of the points, so
  irregular sampling and gaps need no handling; a change point lies **between** two points and
  is reported as the first point of the new segment (`index`, `time`) with `previous_time`. How
  the change happened inside that gap is not known.
- **sigma**: robust, from the first differences, `1.4826 * MAD(diff y) / sqrt(2)`, which a few
  level shifts hardly move. If more than half the differences are zero the standard deviation of
  the differences over `sqrt(2)` is used. A constant series has no change points. `sigma=`
  overrides.
- **Penalty**: `3 ln n` in units of sigma^2 (a BIC-type penalty with a conservative factor);
  `penalty=` overrides.
- **Minimum segment**: 5 points (`min_segment`). With a minimum length the pruning of a
  candidate takes effect `min_segment` steps late, which keeps the result exact. A series of
  fewer than `2 * min_segment` points cannot have a change point: `searched = False`.
- Each `ChangePoint` has the means before and after, the shift and the shift in sigmas.

Measured (seeded): a shift of 3 sigma at point 40 of 80 is found as exactly one change point in
98.5 % of 200 series, at most 2 points off (mean 0.16). White noise of 60 points has no change
point in 97.3 % of 300 series.

**Limitation: independent noise is assumed.** With autocorrelated noise the cost sees slow drifts
as level shifts. Measured once with a scratch script, not asserted: AR(1) series of 100 points had
a spurious change point in about 30 % of runs at phi = 0.3 and about 80 % at phi = 0.5. Day-to-day
body weight is autocorrelated. `penalty` and `sigma` are the levers (pass the long-run standard
deviation as `sigma`); nothing here estimates it. Before change points become findings the wiring
step has to do that or label the result accordingly; this is flagged for the Architect.

**Minimum data.** `detect_changes(daily, metric_key=..., window_end=..., window_days=...)`
evaluates the policy (`Purpose.TREND` by default, or `purpose=`, or an explicit `requirement=`)
on the daily values and returns the `InsufficientData` of the policy untouched, with no search
run, when it fails. Otherwise it searches the days inside the window and the result carries the
`data_quality` of the policy. It uses only the public names of the policy (`evaluate`,
`evaluate_metric`, `Requirement`, `Purpose`, `InsufficientData`).

## Tests

`backend/tests/analysis_methods/` (module names carry `methods_` so they are unique across
`tests/`). Data are **synthetic**: seeded numpy generators and hand-written arrays
(`methods_builders.py`, `SYNTHETIC = True`).

- **Hand-computed references**, with the working in comments: the baseline windows (spike,
  boundaries), the EWMA recurrence, Mann-Kendall on a ramp and on tied values, the Theil-Sen
  slope, interval and intercept of a five-point series, the Hamed-Rao factor of ranks 1 to 10,
  the interval ranks of Sen, and the split and penalty boundary of PELT.
- **Cross-checks**: `scipy.stats.kendalltau` (S, tie-corrected variance, uncorrected p) and
  `scipy.stats.theilslopes` (slope on irregular times); a plain loop implementation of the
  baseline windows and of equation 9 of Hamed and Rao; the O(n^2) recursion for PELT.
  No worked dataset from a paper is reproduced: the expected values are derived from the cited
  formulas.
- **Properties**: invariance to repeating every reading, to sparse versus dense sampling, to
  shifting and rescaling time and values; EWMA unchanged by points that repeat the next value;
  monotone series give a significant trend; outliers do not move the Theil-Sen slope; the result
  of PELT does not depend on the spacing of the points.
- **Seeded simulations**: the error rates above.
- **Irregular sampling and gaps**: windows with too few points are invalid and never invented;
  clustered readings do not dominate the EWMA; the slope is unbiased on gapped calendars; PELT
  is unmoved by a six-week hole.
- **Gate**: `detect_changes` does not call PELT on insufficient data (`pelt` is replaced by a
  function that fails), returns exactly what `evaluate_metric` returns, ignores days outside the
  window, and honours metric policies, purposes and an explicit requirement.

## References

- Mann (1945), Nonparametric tests against trend, Econometrica 13. Kendall (1975), Rank
  Correlation Methods. Theil (1950). Sen (1968), Estimates of the regression coefficient based on
  Kendall tau, JASA 63. Gilbert (1987), Statistical Methods for Environmental Pollution
  Monitoring, chapters 16-17. Helsel and Hirsch (2002), Statistical Methods in Water Resources,
  chapter 12.
- Hamed and Rao (1998), A modified Mann-Kendall trend test for autocorrelated data, Journal of
  Hydrology 204.
- Killick, Fearnhead and Eckley (2012), Optimal detection of changepoints with a linear
  computational cost, JASA 107. Jackson et al. (2005), An algorithm for optimal partitioning of
  data on an interval, IEEE Signal Processing Letters 12.
- Rousseeuw and Croux (1993), Alternatives to the median absolute deviation, JASA 88.
- Eckner (2019), Algorithms for Unevenly-Spaced Time Series: Moving Averages and Other Rolling
  Operators.