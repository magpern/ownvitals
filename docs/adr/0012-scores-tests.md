# ADR 0012: the synthetic test specification

Part of [ADR 0012](0012-scores.md), with the same status. "§n" means a section of ADR 0012.
**Synthetic data only**: hand-written daily values, nights and activity days, and seeded
generators labelled synthetic (`synthetic: true` or a `synthetic_` prefix), never real readings.
The M4 scores issue implements these cases on the pure functions of `ownvitals.analysis.scores`
with an injected day and no database; the wiring issue adds the storage cases.

Each case states the data and the expected result. Numbers are exact unless a tolerance is given;
every threshold is tested at, just below and just above its boundary.

## Shared fixtures

- **`synthetic_his_table.json`**: a band table built for arithmetic, **not** a band table anyone
  should use. `table_id` `synthetic-test-1`, `source` "synthetic test table". Rows:
  - `bmi`, body, sex `any`, ages 18–120: low `{perfect 40, good 30, average 20, bad 10}`, high
    `{perfect 60, good 70, average 80, bad 90}`;
  - `fat_ratio`, body, `female` 18–39 and `female` 40–120, `male` 18–120: each low null, high
    `{perfect 10, good 20, average 30, bad 40}` (the 40–120 female row shifted by +5);
  - `daily_steps`, activity, `any` 18–120: low `{perfect 10000, good 6000, average 3000, bad
    1000}`, high null;
  - `night_hr`, heart, `any` 18–120: low null, high `{perfect 50, good 60, average 70, bad 80}`;
  - `sleep_ahi`, sleep, `any` 18–120: low null, high `{perfect 0, good 10, average 20, bad 40}`.
  Its SHA-256 is registered in the test module only (not in the production constant).
- **Constant series** (every day the same value), **alternating series** (values *m* ± *a* in equal
  numbers, so median *m* and raw MAD *a* are exact) and **sparse series** (one day fewer than a
  requirement, or one day less of span), as in ADR 0010's test specification.
- A **profile** with birth year 1990 and sex `female`, evaluated on 2026-06-15 (age 36).

## §1: shared behaviour

1. Each kind returns exactly one of `computed`, `insufficient_data`, `not_available`,
   `not_supported`, `not_set`; only `computed` has a value, and every other status has `value`
   None and its reasons in `components`.
2. Provenance: two evaluations on the same inputs give byte-identical `components` (canonical
   JSON) and the same `input_fingerprint`; changing one input row's payload hash, or soft-deleting
   one, changes the fingerprint; bumping a stream's revision changes `input_revision`.
3. Input order: the result doesn't depend on the order the rows are passed in (shuffle with three
   seeds).
4. No score function creates a finding or a notification (the wiring issue: a score evaluation
   leaves `findings` and the outbox unchanged).
5. A label test: no user-facing string of the scores package contains "good", "bad", "poor",
   "healthy", "recovered", "strain" or "risk" (a word list, case-insensitive).
6. `test_analysis_purity.py` covers `ownvitals.analysis.scores`.

## §2: Health Improvement Score

**Table loading**

1. `his_band_table` unset: HIS is `not_set` with reason `no_band_table`.
2. The synthetic table loads in the test registry. Each of these variants fails validation and
   gives `not_set` with reason `invalid_band_table`: low boundaries not monotone; high not
   monotone; low `perfect` above high `perfect`; two `female` `fat_ratio` rows whose ages overlap
   (18–40 and 40–120); an added `any` `fat_ratio` row for ages 18–30, which overlaps the `female`
   18–39 row (a female aged 25 would match both); an unknown criterion; a file whose SHA-256 is not registered.

**Criterion scores** (synthetic table, `bmi` row)

3. 50 → 100; 40 → 100; 60 → 100 (the `perfect` range, both ends).
4. 35 → 87.5; 30 → 75; 25 → 62.5; 20 → 50; 15 → 25; 10 → 0; 5 → 0.
5. 65 → 87.5; 70 → 75; 85 → 25; 90 → 0; 120 → 0.
6. One-sided `daily_steps`: 12000 → 100; 10000 → 100; 8000 → 87.5; 4500 → 62.5; 2000 → 25;
   1000 → 0; 500 → 0.
7. `night_hr`: 45 → 100; 55 → 87.5; 75 → 25; 80 → 0.

**Rows by age and sex**

8. Female aged 36: `fat_ratio` uses the 18–39 row (25 % → 62.5); the same reading at birth year
   1980 (age 46) uses the 40–120 row (boundaries +5: 25 % → 75). Age is `d.year − birth_year`:
   evaluated on 2026-01-01 and 2026-12-31 with birth year 1987 both give 39.
9. Sex unset: `fat_ratio` is `not_set` (`sex_unset`); `bmi`, `daily_steps`, `night_hr` are
   computed. Birth year unset: every row is age-banded, so every criterion is `not_set`
   (`birth_year_unset`) and HIS is `not_set`.

**Inputs and minimum data**

10. `bmi`: 30 days of weight alternating 69.0 / 71.0 kg (median 70.0) and a height of 1.75 m:
    BMI = 70 / 1.75² = 22.857…; with 3 days of weight → `insufficient_data`; 4 → computed; no
    height measure ever → `not_available` (`no_height`).
11. `night_hr`: 6 nights → `insufficient_data`; 7 → computed (median of the 7).
12. `afib_ecg_count`: 2 recordings in 365 days → `insufficient_data`; 3 recordings of which one
    meets E1 (`afib_details` 11) → value 1; a recording 366 days before *d* is outside; the `ecg`
    capability `not_authorized` → `not_supported`. Estimated values in `estimated` are never read
    (a recording with only an `estimated` HR and no Withings AFib code counts as not meeting E1).
13. `high_bp_ratio`: 6 groups, of which 2 have systolic 135 (exactly the cut, counts) and 1 has
    diastolic 84 and systolic 130 (does not count): ratio 2/6; with the `us` variant (130/80) the
    group at 130/84 counts too: 3/6. 5 groups → `insufficient_data`. A group with only one of the
    two values is not counted toward the 6.
14. `weekly_active_minutes` (per 7 days, scaled from the days present): 28 days at moderate +
    intense = 60 min each → 1680 ÷ 28 × 7 = **420**; 20 days present at 60 min each and 8 days
    without a record → 1200 ÷ 20 × 7 = **420** (not 300: missing days never count as 0); 20 days
    present, 10 at 30 min and 10 at 90 min → 1200 ÷ 20 × 7 = 420; a day with a record of 0
    minutes counts as present (21 days, 20 at 60 and one at 0 → 1200 ÷ 21 × 7 = 400); 19 days
    with a record → `insufficient_data`.
15. `attrib=1` readings are excluded from every criterion (a day whose only weight is `attrib=1`
    has no daily value).
16. A missing sleep-summary field (no AHI on any night) → `sleep_ahi` `not_available`, never 0.

**Aggregation**

17. Body {`bmi` 100, `fat_ratio` 62.5} → 81.25; Heart {`night_hr` 87.5} → 87.5; Activity {} →
    n/a; Sleep {`sleep_ahi` 75} → 75. HIS = (81.25 + 87.5 + 75) / 3 = 81.25 → displayed **81**,
    verticals 81, 88, n/a, 75 (round half up: 87.5 → 88).
18. Weights: a test table row with `weight` 3 for `bmi` and 1 for `fat_ratio`: Body = (3·100 +
    62.5) / 4 = 90.625.
19. Only one vertical computed → HIS `insufficient_data` (`one_vertical`); two → computed.
20. Nutrition never appears in `components`, as n/a or otherwise.
21. `components` carries, per criterion, the value, unit, score, table id and row index, the
    window and its `data_quality`; "How it's computed" (M6) renders from it alone.

**Tier gate** (wiring issue)

22. With HIS `experimental` (default), an account without the owner-only flag gets no `his` entry
    from the API (absent, not a placeholder); a flagged household account gets it.

## §3: Body Path

**Exact cases** (no noise)

1. 90 days of weight `80.00 − 0.02 · i` (i = 0 … 89), every day: *b* = −0.02, *b*ₗ = *b*ₕ = −0.02,
   *L* = 78.22, σ = 0.3 (the floor *w*/1.5 = 0.45/1.5); h 30 → centre 77.62, interval 77.62 ±
   1.2816 · 0.3 = [77.2355, 78.0045] (tolerance 1e-4); h 60 → 77.02; h 90 → 76.42. `value` is the
   30-day centre.
2. Constant 80.0 kg every day: *b* = 0, centre 80.0 at every horizon, interval 80.0 ± 0.38448.
3. The same series with fat mass: `body_path:fat_mass` uses *w* 0.1 kg (σ floor 0.0667).
4. Horizons are exactly 30, 60 and 90; nothing beyond 90 is produced.

**Minimum data and robustness**

5. 13 days with data spread over the 90 → `insufficient_data` (the 14-day minimum) though TREND's
   8 days pass; 14 days spanning ≥ 54 days → computed; 14 days spanning 53 days (below 60 % of 90)
   → `insufficient_data` (TREND).
6. Outliers: the series of case 1 with +3.0 kg added on every ninth day (days 5, 14, …, 86: 10
   days, 11 %): *b* = −0.02 within 1e-9 (checked with the merged `trend` while writing this ADR),
   and σ is the floor (the residuals' MAD is 0).
7. No change-point cut: flat 80.0 kg for days 0–59, then `80 − 0.05 · (i − 59)` for days 60–89:
   the fit uses all 90 days (`components.window` is the full window) and `components` carries
   `detect_changes`' result.
8. `components` holds *b*, [*b*ₗ, *b*ₕ], *L*, σ, the window used, `detect_changes`' result, each
   horizon's centre and interval, `ar1_phi`, `n_over_n_star` and `data_quality`.

**Calibration** (seeded simulation, kept within the analysis suite's one-minute budget: 200 series
per cell, seed 20261103)

9. Linear truth, slope −0.02 kg/day, noise sd 0.5 kg; cells: white and AR(1) φ 0.6; data every
   day and on a random 16 % of days (at least 14). Coverage of the true day-*e* + *h* value by the
   80 % interval at h 30, 60, 90 is **between 72 % and 95 %** in every cell (the ADR's table, 400
   series per cell, gave 76.5 to 87.5 %; 72 % is about two standard errors below the lowest cell
   at 200 series).
10. The quadrature form is not what the code computes: a test computes both on one fixed series
    and asserts the interval equals the additive form.

## §4: Readiness

**Fixtures.** 28 nights before *d*: durations alternating 7.0 h and 7.5 h (median 7.25, raw MAD
0.25, scale 1.4826 · 0.25 = 0.37065); night heart rate constant 56 (scale = floor 1 bpm); HRV
alternating 40 and 50 ms (median 45, raw MAD 5, scale 7.413).

1. Last night 7.25 h, 56 bpm, no HRV: components 50, 50 → Readiness **50**.
2. 7.25 h, 54 bpm: HR score 50 + 50 · (−1)(54 − 56) / 3 = 83.33…; Readiness (50 + 83.33) / 2 =
   66.67 → **67**.
3. Duration 7.25 + 3 · 0.37065 = 8.36195 h → 100; 8.5 h → 100 (clamped); 7.25 − 3 · 0.37065 →
   0; 5.0 h → 0.
4. HR 59 (three floors above) → 0; 50 → 100 (clamped).
5. With HRV 52.413 ms (45 + 7.413): HRV score 50 + 50/3 = 66.67; Readiness = mean of three.
6. No sleep summary for the night ending on *d* → `not_available` with the mock-up's reason
   `no_sleep_data`, even with activity or watch heart-rate data on *d*.
7. 9 nights in the 28 before *d* → `insufficient_data`; 10 nights spanning 20 days →
   `insufficient_data` (span); 10 nights spanning 21 days → computed.
8. Duration computed, night heart rate missing in the summary → `insufficient_data`
   (`hr_missing`); HRV missing last night → computed from two components, `components.hrv`
   reason `not_reported`.
9. Last night has HRV 60 ms but only 9 of the 28 preceding nights report HRV (duration and night
   heart rate on all 28): Readiness = mean of duration and night heart rate (case 2's values →
   **67**), `components.hrv` reason `baseline_insufficient`; with 10 HRV nights spanning 21 days
   the HRV component is computed and averaged in.
10. The baseline is PRECEDING: changing last night's values doesn't move *m* or *s*; a night 29
    days before *d* doesn't either.
11. The wake-up day assigns the night: a summary ending 2026-06-15 06:40 local belongs to
    2026-06-15 under both day-assignment modes (a fixed zone and per-reading `source_tz`).

## Storage (wiring issue, §5)

1. One row per (owner, kind, day); a recompute replaces it with new provenance in one
   transaction; a concurrent recompute of the same day leaves one row.
2. Forced RLS: another user's scores are invisible in every execution context (ADR 0002's RLS
   tests extended to `scores`).
3. Stale: a row whose `input_revision` or `input_fingerprint` differs from the current inputs is
   returned with the stale state until the recompute.
4. Account deletion removes every `scores` row (the cascade test of T70).
