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

No database is needed. The 287 tests take about 42 s on an idle Windows machine (41.6 s, measured
in the #87 review), most of it the seeded simulations, and longer while other work runs. Keep the
suite under a minute: a new simulation takes its run count from that budget. The long calibration
runs behind the tables below are a script that CI does not run (see Tests).

## Modules

| Module | What |
|---|---|
| `series.py` | `Series` (times in days, `float64` values), `InputSummary` (n and coverage), `mad()`, `MAD_SCALE` |
| `baseline.py` | `rolling_baseline()`: median, MAD and typical range over time windows |
| `ewma.py` | `ewma()`: exponential average weighted by the time gap |
| `trend.py` | `trend()`: Theil-Sen slope, Mann-Kendall test, AR(1) (default) or Hamed-Rao correction of its variance |
| `changepoint.py` | `find_changes()` (PELT, cost scaled by the long-run noise) and `detect_changes()` (PELT behind the minimum-data policy) |

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
`algo_version` of its module (`baseline/1`, `ewma/1`, `trend/2`, `changepoint/2`; PLAN §5). The
window is the one given to `Series(..., window_days=...)` (the period the finding is about, e.g.
90) or, if none, the days from the first to the last day with data, both counted (a window of
days, one more than the elapsed span the minimum-data policy counts). **Bump a module
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
- **Variance correction for autocorrelation.** Positive autocorrelation makes the points carry
  less independent information than `n`, so `Var(S)` is multiplied by a factor `n/n*` (never
  below 1). `correction=` chooses how it is found, and the result says which was used:
  - `Correction.AR1` (**default**, `trend/2`): the factor of an AR(1) model fitted to the
    detrended series. Below.
  - `Correction.HAMED_RAO`: Hamed and Rao (1998), the autocorrelation of the ranks of the
    detrended series at the lags significant at 95 % (Anderson limits), in
    `n/n* = 1 + 2/(n(n-1)(n-2)) * sum (n-k)(n-k-1)(n-k-2) rho_k`. Kept as the published method;
    **it under-corrects** (table below) and is not the default.
  - `Correction.NONE`: the plain test.
- **Theil-Sen** (Theil 1950, Sen 1968): the median of all pairwise slopes `(y_j - y_i)/(t_j - t_i)`
  with the real times, so the slope is in value units **per day**. The confidence interval of Sen
  uses the corrected variance (ranks `(N - C)/2` and `(N + C)/2 + 1`, interpolated, as in Gilbert
  1987 and Helsel and Hirsch 2002), so it widens with the autocorrelation. The intercept is the
  level at `t_ref`, the first time. The slope and the direction are the same whatever the
  correction.
- The result carries `n_over_n_star_raw` (as computed) and `n_over_n_star` (as used), and for
  `AR1` also `ar1_r` and `ar1_phi`.

Decisions the sources leave open:

- **`n/n*` is never below 1.** The published Hamed-Rao factor drops below 1 for negative
  autocorrelation, which would make the test claim more than the plain one; we only inflate.
  (`AR1` cannot go below 1: its coefficient is limited to 0 or more.)
- **The autocorrelation lag counts points in time order.** On a series with gaps a lag is "k
  readings later", not "k days later". The residuals themselves use the real times.
- At most `MAX_POINTS = 3000` points (the `n(n-1)/2` slopes are held in memory); trends are
  computed over windows of weeks to a year.

### The AR(1) correction (#68)

Hamed-Rao uses the sample autocorrelation at the lags that happen to be significant, so it
misses the small, many-lag part of an AR(1) series' autocorrelation, and a short series' estimate
is noisy: at 5 % it still flags 9 % of 100-point AR(1) series with `phi` 0.3 and 15 % with
`phi` 0.6 that have no trend at all (table below). `Correction.AR1` fits one number, `phi`, and uses
it at every lag. The candidates the issue named, and why this one:

| Candidate | Verdict |
|---|---|
| Variance correction with an effective sample size from a fitted AR(1) coefficient (Bayley and Hammersley 1946 give the effective sample size of an AR(1) series) | **Chosen.** Keeps `S`, `Var(S)`, the slope and its interval those of the data, pure and deterministic, and is one number to audit (`ar1_phi`) |
| Trend-free prewhitening (Yue et al. 2002) | Not chosen, not simulated: it tests a different series (the prewhitened one), so `S`, `tau` and Sen's interval would no longer describe the data shown |
| Block bootstrap | Not chosen: needs random numbers (the package has none) and hundreds of resamples per call |

Yue and Wang (2004) also modify the Mann-Kendall variance by an effective sample size, but they
take it from the sample serial correlation of the detrended series, with no model of it. Here the
sample correlations are replaced by `rho_k = phi^k` with one fitted `phi`, in the Hamed-Rao
weights below; the fit, the bias term and the margin are ours. `Correction.AR1` borrows the idea
of an effective sample size from them and is not their method.

The factor is the Hamed-Rao formula with `rho_k = phi^k` at all lags (a long series tends to
`(1 + phi)/(1 - phi)`, the effective sample size of Bayley and Hammersley). `phi` comes from the
detrended series in four steps:

1. Remove the Theil-Sen trend (real times) and rank the residuals, as Hamed-Rao does. Ranks
   bound the influence of an outlier.
2. `r_1`, the lag-1 autocorrelation of the ranks, mapped to the Gaussian scale,
   `r = 2 sin(pi r_1 / 6)` (the relation between a rank and a product-moment correlation of
   normal data, Kruskal 1958, eq. 6.4). For AR(1) data `r` is then near `phi`.
3. `phi = r + (6 + 6 r)/n`. The added term is the **bias** of `r` after a mean and a trend have
   been removed, measured here as about `-(2 + 5.5 phi)/n` (compare Kendall 1954 and Marriott and
   Pope 1954 for the mean alone), plus a **safety margin** of about `4/n`: in a short series `phi` is not
   known well, and guessing low means a false trend. Both constants are calibrated by simulation
   (`calibrate_autocorrelation.py`), not taken from a paper.
4. `phi` is limited to 0 to 0.95 (never below 0, so the test is only ever made more cautious).

What it costs, and a decision for the Architect: **power**. The margin that holds the level
near alpha at `phi` 0.6 for 8 to 30 days with data also inflates the variance of independent
noise (white noise is flagged in 1.6 % to 3.8 % of series up to 100 points and 4.2 % to 4.3 % at
200 and 365, at a level of 5 %; the plain test flags 3.2 % to 5.0 %), so a real trend is found
less often in short series (power table). A curved but monotone trend leaves smooth residuals
after the linear detrending, which look like persistence, so it is penalised the same way: 30
perfectly monotone readings (a sorted normal sample) get `n/n*` 5 to 11 instead of 2 to 3 and
`p` between 8e-4 and 0.02 instead of 1e-9 to 1e-5 (test
`test_a_monotone_series_is_a_significant_trend`).

### What the correction achieves (measured)

`calibrate_autocorrelation.py --runs 40000 trend`, seed 20261031, **40,000 series per cell**
(20,000 at n = 200 and 8,000 at n = 365), no trend at all, alpha 0.05, AR(1) in reading order at
irregular days (the test uses the order, the detrending the times). Percent of series called a
trend; the standard error is 0.1 to 0.2 points (0.1 to 0.5 at n = 200 and 365), so the tables
are good to a few tenths of a point. Every cell has its own generator, seeded from the seed and
the cell (`cell_seed`), so the cells are independent and the result does not depend on the
number of workers. The bound of the acceptance criterion is alpha plus three standard errors of
the series made: 5.33 % at 40,000 series (5.46 % at 20,000, 5.73 % at 8,000).

| n | phi 0: plain / Hamed-Rao / AR(1) | phi 0.3: plain / Hamed-Rao / AR(1) | phi 0.6: plain / Hamed-Rao / AR(1) |
|---|---|---|---|
| 8 | 3.2 / 3.2 / 1.6 | 7.2 / 7.2 / 3.1 | 15.4 / 15.3 / **5.9** |
| 10 | 4.8 / 4.7 / 2.0 | 10.8 / 10.7 / 3.5 | 22.4 / 22.1 / **6.4** |
| 12 | 4.4 / 4.3 / 1.9 | 11.2 / 11.0 / 3.7 | 23.9 / 23.1 / **6.2** |
| 15 | 4.6 / 4.5 / 2.2 | 11.8 / 11.4 / 3.9 | 25.9 / 23.4 / **5.9** |
| 20 | 4.7 / 4.5 / 2.3 | 12.4 / 11.7 / 3.9 | 27.8 / 22.5 / **5.4** |
| 30 | 4.8 / 4.6 / 2.8 | 13.5 / 11.9 / 4.0 | 29.5 / 19.9 / **5.0** |
| 60 | 4.7 / 4.4 / 3.3 | 13.9 / 10.4 / 4.2 | 30.8 / 16.2 / **4.3** |
| 100 | 4.8 / 4.4 / 3.8 | 14.3 / 9.2 / 4.3 | 31.0 / 14.8 / **4.4** |
| 200 | 5.0 / 4.4 / 4.3 | 14.4 / 8.5 / 4.5 | 31.7 / 13.5 / **4.5** |
| 365 | 4.7 / 4.0 / 4.2 | 15.0 / 8.3 / 4.8 | 31.6 / 13.2 / **4.8** |

**At `phi` 0.6 and fewer than 20 points the corrected test calls a trend in about 6 % of series,
not 5 %**: 5.9, 6.4, 6.2 and 5.9 % at 8, 10, 12 and 15 points, 5.4 % at 20, 5.0 % at 30, and 4.3
to 4.8 % from 60 points on. That is about one point above alpha and above the bound of 5.33 %
for 40,000 series (the bound shrinks as the number of series grows; 6.4 % is inside it only for
up to about 2,000 series). It is still far below the 15 to 26 % of the plain test and the 15 to
23 % of Hamed-Rao at 8 to 15 points, and at `phi` 0.3 the rate is 3.1 % to 3.9 % at 8 to 20
points. The same cells had 5.5 % (8 points) and 5.8 % (12) in the earlier version of this table,
which was the first 5,000 series of each cell here (the same seed): a few tenths of a point
low by chance (the standard error of 5,000 series is 0.3). The #87 review's independent run (its
own generator, 42,000 series per cell) gave 5.8 to 6.4 % at 8 to 15 points, as here. The
Architect accepted the method and did not raise the margin, which would cost power where it is
already weakest. M4's findings treat a trend on fewer than 20 points with `phi` above 0.5 as
low-confidence.

These re-baseline the rates of #38 (8.7 % and 13.5 % for Hamed-Rao at 100 points, from one
1000-series run with a shared seed, were 0.6 and 1.2 standard errors below the 9.2 % and 14.8 %
here). Other noise, same bound, 40,000 series (standard error 0.1 to 0.2), `phi` 0.6; plain /
Hamed-Rao / AR(1) in percent:

| Noise | n | plain / Hamed-Rao / AR(1) |
|---|---|---|
| consecutive days instead of irregular ones | 8, 30, 100 | 15.2 / 15.2 / 5.8;  29.9 / 20.2 / 5.0;  31.3 / 15.2 / 4.5 |
| continuous-time AR(1), `phi` 0.6 **per day**, irregular days | 12, 30, 100 | 18.0 / 17.4 / 5.0;  22.3 / 16.5 / 4.2;  23.4 / 12.0 / 4.1 |
| heavy tails: AR(1) with t(3) innovations | 12, 30, 100 | 25.5 / 24.4 / 6.5;  31.2 / 20.1 / 4.4;  32.5 / 15.4 / 3.9 |

Of these, the cells above the bound of 5.33 % are the same short, strongly autocorrelated series:
the consecutive-day series of 8 points (5.8 %) and the heavy-tailed series of 12 points (6.5 %);
the others are at 5.0 % or below. The continuous-time row is the case the model does not describe
exactly (the autocorrelation then depends on the gap between readings, and the fit sees the
average).

Power: white noise plus a straight trend of `drift` standard deviations over the series
(percent of series called a trend; 40,000 series per cell, 8,000 at n = 365; standard error 0.1
to 0.4):

| n | drift | plain | Hamed-Rao | AR(1) |
|---|---|---|---|---|
| 12 | 3.5 | 86.2 | 85.7 | 56.3 |
| 20 | 2.5 | 85.0 | 84.3 | 65.5 |
| 30 | 2.0 | 85.5 | 84.5 | 72.1 |
| 60 | 1.6 | 92.9 | 92.0 | 88.8 |
| 200 | 0.8 | 88.9 | 87.2 | 87.2 |
| 365 | 0.6 | 89.8 | 88.1 | 88.8 |

The tests pin the target on fixed seeds with fewer series (1000, fewer at long lengths) so the
suite stays fast: `phi` 0.3 and 0.6 at n = 8, 15 and 100, and 0.6 at n = 365, within alpha plus
three standard errors of the number of series used (7.07 % at 1000), with a floor (a rate of at
least alpha/4) so that "under the bound" is not "never rejects", and a regression test that
independent noise keeps at least 90 % of the plain test's power at n = 60 (88.8 % against 92.9 %
above). They are the acceptance test; the tables are the evidence. At `phi` 0.6 and 8 or 15
points the true rate is about 6 %, so those two cells pass with a margin of about one point (the
seeded runs give 6.4 % and 5.4 %): they guard against a regression of the margin, not against a
rate of 6 %, and their number of series must not grow past about 2,000 without the bound being
reconsidered.

The acceptance criterion "white noise: no trend in at least 95 % of seeded runs at the stated
alpha" is tested two ways: at alpha = 0.01 the literal 95 % holds; at alpha = 0.05 the rate must
not exceed alpha plus three binomial standard errors (a seeded sample of 1000 cannot promise
95.0 % when the own rate of the test is 5.0 %). All three corrections pass both.

## PELT change points (`changepoint.py`)

PELT (Killick, Fearnhead and Eckley 2012) finds the segmentation of the points in time order
that minimises `sum cost(segment) + penalty * (number of change points)`, pruning the candidates
that can never be optimal again (their Theorem 3.1, `K = 0`). It is **exact**: the tests compare
it with the unpruned O(n^2) optimal-partitioning recursion (Jackson et al. 2005) on random series
with planted shifts and minimum segment lengths 1 to 5, and it returns the same optimum. Two
cases are written out by hand and need no random numbers, with a minimum segment of 2 and of 3
(`test_the_minimum_segment_delay_by_hand_without_a_random_generator`), each checked against the
unpruned recursion and against every partition. A version that prunes at once is returned wrongly
by both, and one that hard-codes the delay to 2 only by the second, so a change of numpy's random
stream cannot stop covering the delay.

The model and its parameters (ours; the paper leaves them open):

- **Cost**: Gaussian mean shift, `sum (y - mean)^2 / sigma^2` per segment (minus twice the
  log-likelihood with a common variance). The cost ignores the spacing of the points, so
  irregular sampling and gaps need no handling; a change point lies **between** two points and
  is reported as the first point of the new segment (`index`, `time`) with `previous_time`. How
  the change happened inside that gap is not known.
- **sigma**: the **long-run** standard deviation of the noise, estimated robustly (below);
  `sigma=` overrides it. A constant series has no change points.
- **Penalty**: `3 ln n` in units of sigma^2 (a BIC-type penalty with a conservative factor);
  `penalty=` overrides.
- **Minimum segment**: 5 points (`min_segment`). With a minimum length the pruning of a
  candidate takes effect `min_segment` steps late, which keeps the result exact. A series of
  fewer than `2 * min_segment` points cannot have a change point: `searched = False`.
- Each `ChangePoint` has the means before and after, the shift and the shift in long-run
  sigmas (`shift_sigmas`). The result has `sigma` and `phi` (None if `sigma=` was given).

### Autocorrelated noise (#68)

With independent noise the cost is the Gaussian likelihood. With positively autocorrelated noise
(day-to-day weight is) a stretch of points has a mean that moves by more than
`sigma/sqrt(length)`, so #38's cost saw slow drifts as level shifts. The cost is now scaled by
the **long-run** variance, the variance of the mean per point, `g (1 + phi)/(1 - phi)` for AR(1)
noise of marginal variance `g` (the quantity Newey and West 1987 estimate without a model).
With it a split's gain on noise alone is about chi-squared with one degree of freedom whatever
`phi` is, which the penalty `3 ln n` is a margin over.

It has to be estimated without being fooled by the shifts it is there to find. It uses
**differences**, which a shift touches at a few places, as Chakar, Lebarbier, Levy-Leduc and Robin
(2017) do to estimate the AR(1) coefficient of a series with mean shifts: theirs is the squared
ratio of the medians of the lag-2 and lag-1 absolute differences, minus 1. The estimator here is
**not simpler than theirs but more elaborate** (five lags, trimmed variances instead of medians,
a curve fit, then a margin and a ceiling), and it is ours. The `v_2/v_1 - 1` variant that was
tried first, below, is the closer analogue of their estimator. The steps:

1. `v_h`, h = 1 to 5: the variance of the lag-h differences `y[i+h] - y[i]`, robust: a value
   beyond 3 robust standard deviations (1.4826 MAD) of the median is left out, and the variance
   of the rest is divided by that of a normal distribution cut at 3 (0.9733). For AR(1) noise
   `v_h = 2 g (1 - phi^h)`.
2. `phi` and `g` from a least-squares fit of that curve over a grid of `phi` from 0 to 0.7 (for
   a given `phi` the best `2 g` is `sum(v w)/sum(w^2)`, `w_h = 1 - phi^h`).
3. `phi` is raised by `1/sqrt(n)` (a margin; capped at 0.7) and `sigma = sqrt(g (1+phi)/(1-phi))`.

The margin and the ceiling are calibrated (`calibrate_autocorrelation.py`), not taken from a
paper. A cheaper estimator was tried first and dropped: `phi = v_2/v_1 - 1` from the lag-1 and
lag-2 differences alone. It is noisier: on white noise of 100 points its 99th percentile of the
estimated sd is 3.1 times the true one, against 1.7 times for the fitted curve, and it let more
spurious change points through at the same margin. The ceiling matters: the long-run variance grows without bound as `phi` goes to 1, which would hide
any shift. Noise more persistent than 0.7 is therefore treated as level changes.

Measured (`calibrate_autocorrelation.py changepoint`, seed 20261031; percent of series, standard
error in the brackets). **Series with no change**, AR(1) noise, a change point reported:

| n | phi 0 | phi 0.3 | phi 0.5 | phi 0.7 | phi 0.8 | phi 0.9 |
|---|---|---|---|---|---|---|
| 30 | 0.4 (0.1) | 1.2 (0.2) | 2.3 (0.3) | 4.2 (0.5) | 5.5 (0.5) | 13.4 (0.8) |
| 60 | 0.2 (0.1) | 1.5 (0.3) | 1.7 (0.3) | 2.2 (0.3) | 4.7 (0.5) | 23.1 (0.9) |
| 100 | 0.1 (0.0) | 0.8 (0.2) | 0.7 (0.2) | 1.6 (0.3) | 4.3 (0.5) | 33.0 (1.1) |
| 200 | 0.0 (0.0) | 0.4 (0.2) | 0.4 (0.2) | 1.0 (0.3) | 4.9 (0.7) | 49.0 (1.6) |
| 365 | 0.0 (0.0) | 0.5 (0.4) | 0.2 (0.2) | 0.5 (0.4) | 3.5 (0.9) | 65.2 (2.4) |

The target of the issue is at most 5 % for phi up to 0.5: met at every length, with 2.3 % the
highest. The same series with #38's noise level (the standard deviation of the differences over
`sqrt(2)`): 1.7 %, **33.1 %** and **80.8 %** at 100 points for phi 0, 0.3 and 0.5 (the 30 % and 80 %
that the issue reported), and 5.7 %, 30.3 % and 57.6 % at 30 points. Beyond the ceiling the noise
is called level changes, as designed: the rate climbs at phi 0.9 (the estimate of the long-run sd
there is 0.33 to 0.36 of the true one, so it cannot be right for such series).
For independent noise the estimate is about 8 % to 26 % above the true standard deviation (median
1.26 at 30 points, 1.14 at 100, 1.08 at 365): the price of not knowing `phi`.

**A shift of 2 long-run standard deviations** at the middle of the series (shift
`2/(1 - phi)` for AR(1) with innovations of sd 1), found as exactly one change point / within 5
points of the shift; the mean distance of the single change point from it is 0.13 to 0.73 points
at every length:

| n | phi 0 | phi 0.3 | phi 0.5 |
|---|---|---|---|
| 30 | 60.3 / 60.2 | 42.4 / 42.1 | 42.5 / 43.0 |
| 60 | 96.1 / 95.5 | 87.6 / 87.3 | 94.7 / 94.8 |
| 100 | 99.9 / 99.2 | 99.3 / 98.9 | 99.8 / 99.8 |
| 200 | 100.0 / 99.2 | 99.4 / 99.0 | 99.4 / 100.0 |
| 365 | 100.0 / 99.0 | 100.0 / 99.0 | 99.5 / 100.0 |

(1000 series per cell up to 100 points, 500 at 200, 200 at 365; more than two change points in
at most 0.2 %.) A shift of 2 long-run standard deviations is a small shift in a short series:
less than half of the 30-point series have it found. The stated tolerance of the seeded test is
one change point exactly in 95 % and within 5 points in 95 % of the 100-point runs; shorter series are
not claimed. #38's figures under the new noise level: a shift of 3 sigma at point 40 of 80 is
found as exactly one change point in 100 % of 200 series (at most 2 points off, mean 0.16);
white noise of 60 points has no change point in 100 % of 300 series.

**Limitation: AR(1) is the model.** Noise with a different shape (a moving-average part, long
memory, a seasonal cycle) is not described by one `phi`; the variogram fit would still follow
its first lags. Real data are for the owner's probe, not for this module.

**Minimum data.** `detect_changes(daily, metric_key=..., window_end=..., window_days=...)`
evaluates the policy (`Purpose.TREND` by default, or `purpose=`, or an explicit `requirement=`)
on the daily values and returns the `InsufficientData` of the policy untouched, with no search
run, when it fails. Otherwise it searches the days inside the window and the result carries the
`data_quality` of the policy. It uses only the public names of the policy (`evaluate`,
`evaluate_metric`, `Requirement`, `Purpose`, `InsufficientData`). `changepoint/2` is used with
`dataquality/2`, the elapsed-day span of the policy (#74): a series that was one day short under
the old inclusive count is refused now.

## Tests

`backend/tests/analysis_methods/` (module names carry `methods_` so they are unique across
`tests/`). Data are **synthetic**: seeded numpy generators and hand-written arrays
(`methods_builders.py`, `SYNTHETIC = True`).

- **Hand-computed references**, with the working in comments: the baseline windows (spike,
  boundaries), the EWMA recurrence, Mann-Kendall on a ramp and on tied values, the Theil-Sen
  slope, interval and intercept of a five-point series, the Hamed-Rao factor of ranks 1 to 10,
  the interval ranks of Sen, the split and penalty boundary of PELT, the AR(1) factor
  (n = 5, phi 0.5: 1.45) and coefficient (ranks 1 3 6 2 5 4), the trimmed variance, the
  variogram fit (phi 0.5, g 1), and two PELT cases (minimum segment 2 and 3) that need no random
  numbers and catch a missing or too short minimum-segment delay.
- **Cross-checks**: `scipy.stats.kendalltau` (S, tie-corrected variance, uncorrected p) and
  `scipy.stats.theilslopes` (slope on irregular times); a plain loop implementation of the
  baseline windows and of equation 9 of Hamed and Rao; the O(n^2) recursion for PELT.
  No worked dataset from a paper is reproduced: the expected values are derived from the cited
  formulas.
- **Properties**: invariance to repeating every reading, to sparse versus dense sampling, to
  shifting and rescaling time and values; EWMA unchanged by points that repeat the next value;
  monotone series give a significant trend; outliers do not move the Theil-Sen slope; the result
  of PELT does not depend on the spacing of the points.
- **Seeded simulations** (`test_methods_trend_ar1.py`, `test_methods_changepoint_ar1.py`, and
  the white-noise tests of `test_methods_trend.py`): the error rates above, on fixed seeds with
  the bound stated in each test (alpha plus three binomial standard errors of the runs made, or
  the 5 % of the issue). `calibrate_autocorrelation.py` is **not a test** and CI does not run
  it: it prints the tables above from larger runs and is how the constants were chosen. The
  trend tables are `--runs 40000 trend` (40,000 series per cell up to 100 points, 20,000 at 200,
  8,000 at 365, the power table 40,000 up to 200): 8,320 s of CPU time summed over its 10
  workers, 43 minutes of wall time on a shared 16-core Windows machine. The change-point tables
  are `changepoint` at its default of 2000 series per cell, a few minutes with 8 processes. Both
  use seed 20261031, and their output is what is recorded in this document (the script prints
  its CPU and wall time on the last line).
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
- Bayley and Hammersley (1946), The "effective" number of independent observations in an
  autocorrelated time series, Journal of the Royal Statistical Society, Supplement 8. Yue and
  Wang (2004), The Mann-Kendall test modified by effective sample size to detect trend in serially
  correlated hydrological series, Water Resources Management 18 (the effective sample size comes
  from the sample serial correlation of the detrended series). Yue, Pilon, Phinney and Cavadias
  (2002), The influence of autocorrelation on the ability to detect trend in hydrological series,
  Hydrological Processes 16 (trend-free prewhitening, not used).
- Kendall (1954), Note on bias in the estimation of autocorrelation, Biometrika 41. Marriott and
  Pope (1954), Bias in the estimation of autocorrelations, Biometrika 41. Kruskal (1958), Ordinal
  measures of association, JASA 53 (eq. 6.4 is the relation between a rank and a product-moment
  correlation of normal data).
- Newey and West (1987), A simple, positive semi-definite, heteroskedasticity and autocorrelation
  consistent covariance matrix, Econometrica 55. Chakar, Lebarbier, Levy-Leduc and Robin (2017), A
  robust approach for estimating change-points in the mean of an AR(1) process, Bernoulli 23.
- Killick, Fearnhead and Eckley (2012), Optimal detection of changepoints with a linear
  computational cost, JASA 107. Jackson et al. (2005), An algorithm for optimal partitioning of
  data on an interval, IEEE Signal Processing Letters 12.
- Rousseeuw and Croux (1993), Alternatives to the median absolute deviation, JASA 88.
- Eckner (2019), Algorithms for Unevenly-Spaced Time Series: Moving Averages and Other Rolling
  Operators.