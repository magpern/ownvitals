# ADR 0012: Scores: Health Improvement Score, Body Path and Readiness

Status: Proposed (#182). The test specification is [0012-scores-tests.md](0012-scores-tests.md),
part of this ADR. Open questions for the owner are listed at the end with the default that
applies until they are answered.

## Context

PLAN §7.1 step 8: "**Scores**: the Health Improvement Score (findings §3 bands), Body Path
trajectory with a prediction interval, and Readiness when sleep or watch data exists. Each one is
versioned with provenance." PLAN §5 gives the table (`scores`: `owner_id, kind, day, value,
components jsonb, algo_version, input_revision, input_fingerprint`) and the provenance rule. ADR
0010 §8 left "Health Improvement Score band table, Readiness, Body Path interval" to a later ADR
and the HIS table to the owner. The owner decided on 2026-10-31 that the health score is shown
on the overview "as a plain number with its components and a 'How it's computed' link"
(`docs/design/README.md`); the Dashboard mock-up shows "Health score · ownvitals 1.0 · 71 / 100 ·
Body 78 · Heart 70 · Activity 64 · Sleep n/a", and a Sleep tile reading "Readiness needs sleep
data, so it isn't computed". Settings carries birth year and "sex for score bands" ("Leave it
unset and those parts aren't computed").

What withings-findings §3 tells us: the app's "His" score cards ship as 42 rows of piecewise
bands per criterion, keyed by age range and sex, with `bad / average / good / perfect` boundaries
on a low and a high side, `max_score` and `precision`, over five verticals (Body, Heart, Sleep,
Activity, Nutrition); `libvitality` builds readiness on personal dynamic baselines (sleep
duration, night HR, HRV); `libbodybalance` fits a weight trajectory with confidence intervals and
a goal date. **We don't run those libraries** (findings §3) and the decompiled table is neither
published nor the user's own history (ADR 0010 §8).

Two constraints bound every score. Trend-tier output uses only the user's own history and
describes, never diagnoses (PLAN §7.2, D9); population or clinical bands are the experimental
tier, behind the owner-only flag on household accounts. And the methods are the merged pure
library (`docs/dev/analysis-methods.md`: `rolling_baseline`, `trend`, `detect_changes`; policy in
`docs/dev/analysis.md`), so nothing here adds a statistical method.

"Our choice" marks an Architect choice with its reason; every other number cites its source.
Examples here and in the test specification are synthetic.

## Decision

### 1. What every score shares

- **Kinds** (`scores.kind`): `his`, `body_path:<metric_key>` (`weight`, `fat_mass`,
  `muscle_mass`), `readiness`. One row per (owner, kind, day).
- **Day and evaluation day** are ADR 0010 §1's: a local calendar day under the user's day
  assignment; a score is evaluated on a day *d* when one of its inputs has a daily value (or a
  sleep summary, or an activity day) on or before *d* that is new or changed; backfill evaluates
  every such day in order. Scores are evaluated after the finding rules of the same day.
- **Status.** Every evaluation yields one of `computed`, `insufficient_data` (a minimum-data
  requirement failed: the shortfalls are kept), `not_available` (an input stream has no data for
  this user, e.g. no sleep summaries), `not_supported` (the capability is unsupported or not
  authorised, PLAN §4.2) or `not_set` (the profile lacks birth year or sex where a part needs it;
  or, for HIS, no approved band table, §2). Only `computed` has a `value`; the others store
  `value` NULL with the status and reasons in `components`, so the UI can show the data state and
  its provenance. **A missing value is never 0 or "normal"** (PLAN §4.2).
- **Provenance** (PLAN §5): `algo_version` per kind (`his/1`, `bodypath/1`, `readiness/1`, plus
  the versions of every method module used, in `components.modules`), `input_revision` (the
  `data_revisions` counters of every stream read) and `input_fingerprint` (SHA-256 over the sorted
  ids and payload hashes of the exact input rows). A score is stale when either differs; a stale
  score is recomputed, and until then it is shown with the "stale" data state, never silently.
- **Never notifications.** Scores never notify and never create findings. A score change is not an
  event.
- **No diagnostic labels.** No score is ever shown with a word such as good, bad, poor, healthy,
  recovered or at risk; the band names of §2 are internal. Text describes ("compared with your
  last 28 nights").
- **Tier of each score**:

| Score | Tier | Why |
|---|---|---|
| Body Path | **trend** | A projection of the user's own series (PLAN §7.2's "comparison with the user's own history") |
| Readiness | **trend** | Components compared with the user's own recent nights only |
| Health Improvement Score | **experimental** (default; owner question 2) | Its bands are population bands by age and sex from Withings' decompiled card: PLAN §7.2 puts "clinical thresholds … and interpretations" in the experimental tier |

- **Body Path stretches "descriptive".** PLAN §7.2 defines the trend tier as a descriptive
  comparison with the user's own history; a 30/60/90-day projection looks forward. It stays in
  the trend tier because PLAN §7.1 step 8 asks for exactly this score with a prediction interval,
  it uses only the user's own series, it covers body mass only (no clinical threshold, no
  interpretation of what the value means), and its text says it is not a prediction (§3). This is
  the less restrictive of the two possible tiers, chosen knowingly; the owner can move it to the
  experimental tier, which would only hide it (a setting change, no data change).

- **Purity.** The score functions live in `ownvitals.analysis.scores`, covered by
  `test_analysis_purity.py`: no database, clock or randomness; the wiring issue reads rows and
  writes `scores`.

### 2. Health Improvement Score (HIS)

**Structure** (our choice, following the shape findings §3 describes; the values are the owner's):

- **A band table** is a JSON data file, `analysis/scores/his_tables/<table_id>.json`, with
  `table_id`, `version`, `source` (free text naming where its values come from), and a list of
  rows: `criterion`, `vertical`, `sex` (`any`, `female`, `male`), `age_min`, `age_max`
  (inclusive), `unit`, `low` and `high` (each either null or four boundaries
  `{perfect, good, average, bad}`), and `weight` (default 1). A loader validates it: boundaries
  monotone (`bad ≤ average ≤ good ≤ perfect` on the low side, the reverse on the high side, low
  `perfect` ≤ high `perfect`), at most one row matching any (criterion, sex, age): the age
  ranges of a criterion's rows must not overlap within one sex, and an `any` row must not overlap
  in age with a `female` or `male` row of the same criterion, so no user ever matches two rows;
  every criterion
  known to the code (below), and the file's SHA-256 equal to a constant in the code (a table
  changes only through a reviewed PR).
- **Which table is used** is the setting `his_band_table` (a `table_id`), **default unset**. Unset,
  or a table that fails validation: HIS is `not_set` ("Score bands not set") for everyone, and
  nothing else changes. This is the "behind a setting" the issue asks for: the code ships, the
  table is the owner's (open question 1).
- **Criterion score** (our choice): piecewise linear in the value, with anchors **perfect = 100,
  good = 75, average = 50, bad = 0** at the boundaries, 100 between the low and high `perfect`
  boundaries, and clamped to 0 beyond `bad`. A one-sided criterion (steps: higher is better) has a
  null side and scores 100 beyond its `perfect` boundary. Why linear anchors: the decompiled
  `max_score` and `precision` semantics are unknown, and equal steps between bands are the
  simplest monotone reading; a table may later carry its own anchors (a schema version bump).
- **Vertical score** = the weighted mean of the vertical's computed criterion scores; a vertical
  with none computed is `n/a`. **HIS** = the mean of the computed vertical scores, equal weights,
  shown as an integer 0 to 100 (round half up) with each vertical's integer, as in the mock-up.
  At least **two verticals** must be computed (our choice: one vertical would just repeat its tile),
  otherwise HIS is `insufficient_data`.
- **Nutrition** has no public-API input: it is never shown (not even as n/a).
- **Rows used** are those whose `sex` matches the profile (or `any`) and whose age range contains
  the user's age at *d*, `age = d.year − birth_year` (the profile holds only the year; at most one
  year off, our choice over asking for a birth date). Birth year unset: every age-banded criterion
  is `not_set`. Sex unset: every sex-specific criterion is `not_set` (Settings' wording).

**Criteria and inputs** (windows and minimums are our choice: long enough to be stable, short
enough to reflect "now"; each is a parameter with a version):

| Criterion | Vertical | Value at *d* | Minimum data | Source of the input |
|---|---|---|---|---|
| `bmi` | body | median `weight` daily value over the 30 days ending *d* ÷ (latest height)² | ≥ 4 days with weight; a height measure (type 4) ever | weight (1), height (4) |
| `fat_ratio` | body | median `fat_ratio` daily value, 30 days | ≥ 4 days | type 6 |
| `night_hr` | heart | median sleep-summary average heart rate, 30 nights | ≥ 7 nights | sleep summaries |
| `vascular_age_gap` | heart | median `vascular_age` daily value over 90 days − age | ≥ 2 days | type 155 |
| `afib_ecg_count` | heart | number of recordings in the 365 days ending *d* meeting E1's condition (ADR 0006 §11) | ≥ 3 recordings in the window; the `ecg` capability supported | `ecg_recordings` |
| `high_bp_ratio` | heart | share of blood-pressure groups in 90 days with systolic ≥ 135 or diastolic ≥ 85 mmHg (findings §3, EU variant; `his_bp_variant` setting `eu`/`us`, US 130/80) | ≥ 6 groups with both values | types 10, 9 |
| `daily_steps` | activity | median steps per day, 30 days | ≥ 14 days with an activity record | `activity_daily` |
| `weekly_active_minutes` | activity | (moderate + intense minutes summed over the days with an activity record in the 28 days ending *d*) ÷ (number of those days) × 7: minutes per 7 days, scaled from the days present, so a missing day never counts as 0 minutes (PLAN §4.2) | ≥ 20 days with an activity record | `activity_daily` |
| `sleep_ahi` | sleep | median apnea–hypopnea index, 30 nights | ≥ 7 nights reporting it | sleep summaries |

- Inputs are the quality-gated usable readings (#37; `attrib=1` excluded, PLAN §7.1).
  `afib_ecg_count` counts Withings' own classification only, never our estimates (ADR 0006 §9).
- Every criterion row in `components` carries its value, unit, score, the band row used (table id,
  row index), the window and its `data_quality`. "How it's computed" renders exactly that, the
  table's `source` and version, and the sentence "This score compares your values with fixed
  bands for your age and sex; it is not a medical assessment."
- Fields whose public-API names are not confirmed (the sleep summary's average heart rate and
  AHI, activity's moderate and intense durations) are read through the wiring issue's mapping;
  until the owner's probe confirms them (PLAN-operations §10.4) they are **documented assumptions**
  in `docs/status.md`, and a missing field gives `not_available`, never 0.

### 3. Body Path

A projection of the user's own recent trajectory, with an 80 % prediction interval.

- **Metrics:** `weight`, and `fat_mass` and `muscle_mass` where the user has them (body
  composition "where present"). Each is its own kind.
- **Fit window** (our choice): the **90 days** ending *d* (the TREND window of ADR 0010), daily
  values (#37), `Series.from_daily`. **No change-point cut in v1**: the merged PELT
  (`changepoint.py`) models shifts in the mean, so a steady decline is cut into several level
  segments and fitting "after the last change point" would throw away the very trend to project.
  The Theil–Sen slope is robust to isolated outliers instead (a breakdown point of about 29 %),
  and the text says trends change (below). `components` records `detect_changes`' result for the
  window so the "How it's computed" view can show a recent level shift next to the band.
- **Minimum data** (our choice): the metric's TREND requirement (ADR 0010, `policy.py`: ≥ 8 days
  with data spanning ≥ 60 % of the window) **and at least 14 days with data**, so the residual
  scale has enough points. Below: `insufficient_data` with the progress the design's
  insufficient-data state shows.
- **Fit:** `trend(series, alpha=0.2, correction=AR1)`: the Theil–Sen slope *b* and Sen's 80 %
  slope interval [*b*ₗ, *b*ₕ] widened by the AR(1) correction (#68). The level at the window's last
  day *e* is *L* = `intercept + b · (e − t_ref)`. The residual scale σ = max(1.4826 · MAD of the
  residuals `y − (intercept + b · (t − t_ref))`, *w* / 1.5), where *w* is the metric's minimum
  half-width of ADR 0010 §2 (weight 0.45 kg, muscle mass 0.3 kg, fat mass one display step,
  0.1 kg): the display band's `min_scale`, so a noise-free series never gets a zero-width
  interval. The calibration below was run without the floor;
  with noise sd 0.5 kg against a floor of 0.3 kg it can only widen an interval.
- **Projection** at horizon *h* ∈ {30, 60, 90} days after *e* (our choice: never beyond the fit
  window's length):
  - centre `L + b · h`;
  - **80 % prediction interval** `[L + bₗ · h − z · σ, L + bₕ · h + z · σ]`, z = 1.2816 (the
    normal 90th percentile). The slope's interval and the day-to-day noise are added, not combined
    in quadrature: deliberately conservative.
- **Calibration** (synthetic check with the merged `trend`, seed 20261103, 400 series per cell,
  noise sd 0.5 kg, a linear truth with slope 0 or −0.02 kg/day, 90-day fit window; coverage of the
  true value on day *e* + *h*, median interval width in kg):

| Noise | Data on | h 30 | h 60 | h 90 |
|---|---|---|---|---|
| white | every day | 82.5 % (1.42) | 86.8 % (1.58) | 85.5 % (1.76) |
| white | 70 % of days | 82.2 % (1.45) | 87.5 % (1.66) | 86.2 % (1.87) |
| white | 16 % of days (≈ 14) | 79.0 % (1.66) | 82.8 % (2.16) | 86.2 % (2.66) |
| AR(1) φ 0.6 | every day | 80.5 % (1.54) | 84.8 % (1.89) | 86.8 % (2.22) |
| AR(1) φ 0.6 | 70 % of days | 79.8 % (1.56) | 85.0 % (1.92) | 84.0 % (2.28) |
| AR(1) φ 0.6 | 16 % of days | 76.5 % (1.67) | 84.5 % (2.23) | 85.2 % (2.79) |

  Coverage doesn't depend on the slope (the same seeds gave identical rows for 0 and −0.02). The
  standard error is about 2 points. The quadrature alternative (`L + b·h ± z·√(σ² + (h·se_b)²)`)
  covered only 62 to 77 %, under its nominal 80 %, so it was rejected. **This calibration holds
  for a straight-line truth only**: a real trajectory that bends after *e* is not covered, which
  the text says (below).
- **No goal date and no target** (findings §3's goal completion date is not rebuilt): ownvitals is
  an analytics tool, not coaching (PLAN §12, PLAN-operations §9.4).
- **Text:** "If the last *n* days' trend continued, your weight on <date> would likely be between
  *a* and *b* kg (8 times in 10). Trends often change; this is not a prediction of what will
  happen." Shown as a band continuing the weight chart from *e*, labelled "Body Path". Not dashed:
  dashes mark the experimental tier in the design. The mock-ups have no Body Path, so the M6 issue
  takes its look to the Architect (AGENTS.md "UI design").
- `components` holds *b*, [*b*ₗ, *b*ₕ], *L*, σ, the window used, each
  horizon's centre and interval, `trend`'s `ar1_phi` and `n_over_n_star`, `detect_changes`'
  result (or its insufficient-data reason), and `data_quality`;
  `value` is the 30-day centre.

### 4. Readiness

How last night compares with the user's own recent nights. **Only when a sleep summary exists**
for the night ending on *d*; otherwise `not_available` with the mock-up's text ("Readiness needs
sleep data, so it isn't computed"). A watch without sleep tracking gives no Readiness (our reading
of PLAN §7.1's "sleep or watch data": every watch component we use comes from the sleep summary).

- **Components** (from the sleep summary of the night whose wake-up falls on *d*, local day):
  sleep duration (total sleep time, hours), night heart rate (the summary's average), and **HRV**
  (the summary's RMSSD) only where the device reports it. Field names are documented assumptions
  until the probe confirms them (§2).
- **Baseline** (our choice, after findings §3's "personal dynamic baselines"): the **28 nights
  before** *d* (PRECEDING), median *m* and scale *s* = max(1.4826 · MAD, *w*) per component, with
  the BASELINE requirement (≥ 10 nights spanning ≥ 21 days); below it the component is
  `insufficient_data`, and a component whose own baseline fails is `insufficient_data` even if
  last night has a value. *w* (our choice, one display step): 0.25 h, 1 bpm, 2 ms.
- **Component score** = `clamp(50 + 50 · σ_c · (x − m) / (3 s), 0, 100)`, with direction
  σ_c = +1 for duration and HRV, −1 for night heart rate. **The directions are our choice**:
  findings §3 lists the recovery indicator's inputs (sleep duration, night HR, HRV), not how each
  moves it; longer sleep, a lower night heart rate and a higher HRV than usual are the common
  reading. 50 means "a typical night for you"; three scales away reaches 0 or 100. Our choice:
  symmetric and bounded, so no single night dominates. Duration has no upper turning point: an
  unusually long night (after illness, say) scores up to 100 on it; the text shows the hours, so
  the reader sees why.
- **Readiness** = the mean of the computed components, equal weights, as an integer; duration and
  night heart rate must both be computed. HRV is added only when it is computed: last night has
  a value **and** its own 28-night baseline passes. Otherwise Readiness is the mean of the other
  two, and `components.hrv` gives the reason (`not_reported` when last night has no value,
  `baseline_insufficient` when the baseline fails). Duration or night heart rate not computed:
  `insufficient_data`.
- Text: "Compared with your last 28 nights: sleep 7 h 10 (usual 7 h 25), night heart rate 54 (usual
  56), HRV 48 ms (usual 45)". No "recovered" or "strained" label. The vitals malus of findings §3
  (night temperature, SpO2, respiratory rate) and the effort indicator are not in v1 (§6).

### 5. Storage and display (for the wiring and M6 issues)

- `scores` as PLAN §5, with `value` nullable and `components.status` (§1); one row per (owner,
  kind, day); a recompute overwrites the row of the same day in the same transaction as its
  provenance. Retention: kept like `metric_daily`.
- The HIS tile shows the latest computed day's score with the date it was computed for; when the
  last computed day is older than 7 days, the stale state, not the number alone.
- The API returns status, value, components and provenance; the experimental HIS only to a
  session whose account has the owner-only flag (ADR 0010 §7's gate); every other account gets
  nothing for `his`, not a placeholder.

### 6. Not in v1

| Item | Why | Who settles it |
|---|---|---|
| Nutrition vertical | no public-API input | — |
| Readiness' vitals malus and effort indicator | inputs (night temperature, SpO2, respiratory rate) vary by device; no defined method | a later amendment |
| Body Path goal date | coaching; PLAN §12 | the owner (a product decision) |
| Body Path on fat ratio | a ratio of two noisy masses; the masses are projected instead | — |
| HIS criteria not in findings §3's abbreviated table | the full 42-row table is not in the repository | the owner, with the table |

## Alternatives

- **Ship HIS with findings §3's abbreviated table as the default.** It has gaps between bands and
  only the 18–40 bracket for most rows, it comes from a decompiled proprietary app, and using it
  is a question under PLAN-operations §9.4 ("substantially similar") and possibly copyright.
  Rejected as a default; the owner decides (question 1).
- **HIS in the trend tier.** Its bands are population bands, not the user's history; PLAN §7.2
  would need an amendment. Kept as owner question 2.
- **An HIS from personal baselines only** (each criterion scored against the user's own range).
  It would be trend-tier, but it can't say anything about level (a stable BMI of 35 would score
  like a stable 22), which is the point of the Withings score. Rejected for v1; possible later
  as a separate score.
- **Body Path by an exponentially weighted local level and trend (Holt) or a state-space model.**
  Better for bending trajectories, but a new method outside the merged library and with no
  calibration here. Rejected for v1.
- **Body Path fitted after the last change point.** PELT detects mean shifts, so a ramp becomes
  several segments and the last one is too short to fit: the cut would refuse exactly the users
  whose weight is moving. Rejected (§3); a slope-change detector would be a new method.
- **Body Path interval in quadrature.** Under-covers (62 to 77 % for a nominal 80 %; §3).
- **95 % interval.** About half as wide again as the 80 % one (z 1.96 against 1.28, and a wider
  slope interval), so too wide to read at 90 days; 80 % with "8 times in 10" is the plainer
  statement.
- **Readiness as a weighted z-sum without a cap.** One unusual night would dominate; the clamp
  keeps it bounded.

## Consequences

- The M4 scores issue implements `ownvitals.analysis.scores` (§2 to §4) with the test
  specification; the wiring issue reads the inputs, evaluates on evaluation days, writes `scores`
  and adds the profile fields; M6 builds the tiles, the "How it's computed" view and the Body Path
  band.
- With the defaults, **HIS is `not_set` for everyone** and invisible to accounts without the
  experimental flag. The Dashboard's health-score tile then shows "Score bands not set" to the
  flagged owner and is absent elsewhere; the Architect raises the mock-up difference with the owner
  in the same status entry as questions 1 and 2.
- New documented assumptions in `docs/status.md`: the sleep-summary heart-rate, HRV and AHI field
  names and the activity intensity fields (§2, §4).
- Every number here is a parameter with a version: a change is a dated amendment to this ADR and
  bumps the kind's `algo_version`.

## Open questions

**For the owner** (the Architect records them in `docs/status.md` "Blocked on the owner"):

1. **The HIS band table.** Which values, from which source (the decompiled card of findings §3,
   a published reference, or the owner's own), for which age and sex brackets, and whether using
   the decompiled card is acceptable under PLAN-operations §9.4 and its licence. Default
   meanwhile: **no table**; `his_band_table` unset, HIS `not_set`. Safe-off: unset.
2. **The HIS tier.** Experimental (owner-only flag, household accounts) or visible to every
   household account as the mock-up shows? The latter needs a roadmap amendment because the bands
   are not the user's own history (PLAN §7.2). Default meanwhile: **experimental**. Safe-off:
   experimental.
3. **The HIS anchors** (perfect 100, good 75, average 50, bad 0, linear) and the "at least two
   verticals" rule, if the owner's table comes with its own scoring. Default meanwhile: as §2.
4. **Body Path goal date.** Not built (§6). Default: none.

**Decided by the Architect (2026-11-03, this ADR).** The statuses and provenance (§1); the
tiers of Body Path and Readiness; the HIS table structure and loader, its criteria, windows and
minimums; the Body Path fit window (no change-point cut), minimum data, 80 % additive interval and
horizons; Readiness' components, 28-night baseline, scale floors and score formula.
