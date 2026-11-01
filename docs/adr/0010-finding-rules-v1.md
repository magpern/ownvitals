# ADR 0010: v1 finding rules: parameters, definitions, confidence

Status: Proposed (#110). Architect's decisions of 2026-11-01 on the first pass are recorded in the
issue and applied here. The test specification is
[0010-finding-rules-v1-tests.md](0010-finding-rules-v1-tests.md), part of this ADR; the sources
are listed at its end. Roadmap Amendment 3 (`findings.tier` gains `system`) has this ADR as its ADR.

## Context

PLAN §7.2 names the v1 finding rules and their tiers (D9), and PLAN §7.1 the pipeline: quality
gates, daily values, the minimum-data policy, robust methods, `confidence` and `data_quality` on
every finding, and `live`/`backfill` origins. Several terms in §7.2 are not computable as written
("rising", "for ≥ 3 days", "significant 90/180-day decline", "resting HR", "low battery", the
extracellular-water rise, segmental asymmetry, the left/right pairing, TBW), and nothing says how
`confidence` and `severity` are computed. The rule code can't invent them, so this ADR fixes them
before it is written, as ADR 0006 did for ECG.

The libraries it builds on are merged (#37, #38, #56, #68, #76, #99): `quality.assess`,
`daily.daily_values`, `dataquality.summarise`, `policy.evaluate` and `evaluate_metric`,
`methods.baseline.rolling_baseline`, `methods.trend.trend` and `methods.changepoint.detect_changes`
(`docs/dev/analysis.md`, `docs/dev/analysis-methods.md`). ADR 0006 owns the ECG signal side and
the logic of E1 to E6; this ADR adds their confidence, severity, origin and E6's band.

Two rules bound everything. **Trend-tier rules use only the user's own history**: a fixed number in
a trend rule (5 %, 2 %, 10 bpm, 10 %, 20 %) is a product parameter of PLAN §7.2 applied to the
user's own values, never a population band and never a clinical claim; their text describes and
never diagnoses (PLAN §7.2). **Every experimental-tier threshold cites a published source**; one
whose source can't be verified is not in v1 (§8). "Our choice" marks an engineering choice with
no literature source. Bounds are inclusive unless a rule says "more than" or "above". Examples
here and in the test specification are synthetic.

## Decision

### 1. Conventions

- **Day.** A day is a local calendar date under the user's day assignment (Settings: each
  reading's own `source_tz`, #37's `Reading.local_date`, or one fixed zone, which the wiring issue
  applies by giving every reading that zone). **Today** is the date of the injected `now` in the
  fixed zone, or in per-reading mode in the `source_tz` of the user's most recent usable reading
  (UTC when there is none).
- **Daily value.** Unless a rule says otherwise, a rule reads #37's `daily_values` of
  `assess(...).usable` (the first morning reading 04:00–11:00, otherwise the median). Its
  **trigger provenance** is ADR 0006 §11's: the morning reading's group, or every group the
  median used; the wiring issue carries the group ids on each daily value.
- **Evaluation day.** A rule is evaluated at day *d* only when its trigger metric has a daily
  value on *d* that is new or changed (backfill evaluates every such day in order). Q1 is
  evaluated on *today* and on every state change it reads. The **window** of a firing evaluation
  is the days its condition used, ending at *d*.
- **Windows** are calendar days. "The *n* days before *q*" are days *q* − *n* … *q* − 1. A policy
  check on them is `evaluate_metric(key, purpose, daily, window_end=q − 1, window_days=n)`; the
  matching band is `rolling_baseline(series, window_days=n, window=Window.PRECEDING, at=[q])`
  on `Series.from_daily` (whole days, so the boundary is exact).
- **Metric keys** are `quality.METRICS`' keys. Keys the wiring issue adds (with engineering
  bounds, #37's rule) are named in §7: segmental muscle by position, the per-foot nerve types.

### 2. The typical range: two bands

Both bands use the **median** and the **scaled MAD**, `scale = max(1.4826 · MAD, min_scale)`
(`MAD_SCALE`; Rousseeuw and Croux 1993), over 90 calendar days by time, never by point count.

| | Display band | Finding threshold |
|---|---|---|
| Used for | charts, tiles, range bars, "your typical range was a–b" | "above/below the personal 90-day range": E6, N2, B1's ECW |
| k | **1.5** | **2.5** (Leys et al. 2013) |
| Window | chart: TRAILING, days *q* − 89 … *q* (`window_days=89`); a comparison sentence: the 90 days before the episode's reference day (PRECEDING) | the 90 days before the episode's **reference day** (PRECEDING) |
| Gate | the metric's BASELINE policy on the same days; below it no band is drawn | the metric's BASELINE policy; below it the rule is "insufficient data", never a finding |

- **Reconciliation with the design and `baseline.py`.** The design's "median ± 1.5 MAD over 90
  days" is computed by the owner-accepted mock-ups as `max(bandMin, 1.4826 · MAD · 1.5)` over a
  trailing 90-day window (`docs/design/mockups/src/*.dc.html`): its MAD is already the scaled MAD,
  the same scaling as `baseline.py`, and its `bandMin` floors the half-width, which this ADR
  adopts (below). Only k differs from `baseline.py`: `DEFAULT_K = 2.0` is used by neither band,
  because every caller passes k. No design change.
- **Reference day.** The first day of the window that made an episode fire (§4): a C1 or N2 run's
  first day, B1's start of the gain, E6's or a guided N2 reading's own day. The band stays the
  one at that day for the whole episode, so a run never moves its own reference.
- **Minimum half-width** *w*: both bands are `median ± max(w, k · 1.4826 · MAD)`, the mock-ups'
  formula with their `bandMin` as *w* (Architect, 2026-11-01), so a window of identical values
  never gives a zero-width band. A caller gets it from `rolling_baseline` with
  `min_scale = w / k`. The same *w* serves both bands. From the mock-ups: weight 0.45 kg, fat
  ratio 0.35 %, muscle mass 0.3 kg, visceral fat index 0.5, heart pulse 2 bpm. Every other metric
  uses one display step (our choice): 0.1 kg for fat-free mass and body water; 1 for metabolic
  and vascular age (years), QTc and QRS (ms), the nerve scores and ESC (µS). On integer-valued
  metrics with a constant window, a step of one display unit then already crosses the finding
  threshold (*w* is the floor of both), a consequence of using one *w*.
- **A finding value is always outside the displayed band**: same median, same *w*, larger k, so
  the finding half-width is never smaller.

**Expected false-out-of-range rate** (synthetic check with the merged `rolling_baseline`, seed
20261031, 200 series × 250 evaluation days per cell; series with no change at all; data on every
day; the noise models of `docs/dev/analysis-methods.md`, AR(1) in day order). Percent of
evaluations outside / above the band computed over the 90 days before:

| k | white noise | AR(1) φ 0.3 | AR(1) φ 0.6 | AR(1) φ 0.6, t(3) innovations |
|---|---|---|---|---|
| 1.5 (display) | 14.8 / 7.5 | 14.6 / 7.3 | 15.3 / 7.6 | 18.8 / 9.5 |
| 2.0 (`DEFAULT_K`) | 5.7 / 2.9 | 5.4 / 2.7 | 6.0 / 2.9 | 10.1 / 5.0 |
| **2.5 (findings)** | **1.8 / 1.0** | **1.9 / 0.9** | **2.0 / 0.9** | **5.8 / 2.8** |
| 3.0 | 0.6 / 0.3 | 0.6 / 0.3 | 0.7 / 0.3 | 3.5 / 1.7 |

With data on a random 70 % of days the rates are 0.1 to 0.7 points higher (k 1.5 white: 15.5 /
7.8; k 2.5: 2.2 / 1.1). Three consecutive days above the band of the first of them, at k 1.5 /
2.0 / 2.5: white 0.07 / 0.01 / 0.00 %; AR(1) φ 0.6 1.25 / 0.31 / 0.06 %; t(3) 2.0 / 0.8 / 0.32 %.
The standard error is about 0.1 point at 7 %. The normal-theory rates are 13.4, 4.6, 1.2 and
0.27 % outside: the estimated median and MAD of a 90-day sample add the rest. So the display
band calls about one day in seven "outside your typical range" on a series that hasn't changed,
which is a description, not a finding; a single-value finding at k 2.5 fires on about 1 % of
values (one-sided), and on about 3 % with heavy tails.

### 3. The undefined terms of PLAN §7.2

| Term | Computable definition |
|---|---|
| "for ≥ 3 days" (C1) | A **run**: at least 3 consecutive calendar days, each with a daily value that meets the condition against the band at the run's first day. A day without data ends the run (gaps stay gaps). |
| "rising" (B5, C2) | `trend(Series.from_daily(window, window_days=90))` with `Correction.AR1` and α 0.05 gives `Direction.INCREASING`, on the 90 days ending at *d*, which pass the metric's TREND policy. An episode needs §4's persistence (21 calendar days, at least 6 evaluations). |
| "significant 90/180-day decline" (N3) | `trend` (AR(1)) gives `DECREASING` on the 90 days or on the 180 days ending at *d*, each window passing the nerve TREND policy (N1), at **α 0.025 per window** (Bonferroni over the two; Dunn 1961); an episode needs §4's persistence. |
| Extracellular-water rise (B1) | The ECW (168) daily value on *d* is above the ECW finding threshold (§2) at the gain's start day. |
| "resting HR" (C1) | Heart pulse, type 11 (`heart_pulse`), as #37's daily value as defined: when several devices report on one day (a Body Scan standing, a blood-pressure monitor seated, a watch), the earliest morning reading wins whatever its device, otherwise the median of all of them. A day's value can therefore come from a different device and posture than the baseline's; v1 accepts that, and keying C1's baseline to the source model is an option the M4 wiring issue may take by amendment. It is **not a clinical resting heart rate**; the trend tier compares it only with the user's own history under the same habit. The label "resting heart rate" is the design's. Night HR from sleep summaries is not in v1 (§8). |
| "low battery" (Q1) | `devices.battery == "low"`, from the public `user getdevice` enum `high`, `medium`, `low` (as `backend/tests/withingsshapes.py` encodes the OpenAPI file). The public API gives no percentage. |
| Segmental asymmetry (B6) | Segmental muscle (175) at positions left arm 3 / right arm 2 and left leg 10 / right leg 11 (findings §2). For each measure group holding both of a pair, SI = \|L − R\| / ((L + R)/2): the magnitude, as a fraction, of the symmetry index as commonly cited from Robinson et al. (1987), (R − L)/(½(R + L)) · 100 %. Fat (174) and fat-free (173) aren't used: fat-free mass largely repeats muscle, and fat mass is not a symmetry measure. |
| Left/right pairing (N5) | Types 158/159 and 197/198, each a pair (findings §2; not in the current public spec). The same symmetric index per measure group holding both of a pair; which member is left is unverified and doesn't matter to a symmetric index (the text says "one foot … the other"). No group with both of a pair: N5 is **not supported** for the user, never "no difference". |
| TBW (B3) | Not a Withings type. TBW = ECW (168) + ICW (169) **of the same measure group**, exact by definition. A group without 169 gives no ratio; a user without one is **not available** for B3. |

### 4. Episodes and origin

- **Subject.** Each rule has a subject: a metric key (E6: `qtc_interval` and `qrs_duration`
  separately; N2/N3 each nerve key), a pair (B6: arms, legs; N5: each type pair), a device (Q1
  battery: `hash_deviceid`), the connection (Q1 reconnect), a stream (Q1 import) or the user (Q1
  no weigh-in).
- **Episode.** At most one **active** finding per (owner, `rule_id`, subject). An evaluation that
  fires while one is active updates it: `window` grows to the new window's end, `evidence`,
  `confidence`, `data_quality`, provenance and `last_seen` are the latest firing's; its reference
  day and band (§2) stay. The first evaluation of the subject that doesn't fire (or is
  insufficient data) **ends** it (`status` ended); a later firing opens a new episode. A recompute
  after a data revision (PLAN §5) re-derives episodes; one with the same rule, subject and
  reference day keeps its `first_seen` and origin.
- **Origin** is a pure function `origin(window_end, today)`: `live` if
  `(today − window_end).days ≤ 6` (the window ends in the last 7 days, today included; a window
  end after today, from a zone ahead of the user's, is live), otherwise `backfill`. It is computed
  once, when the episode is first written, from the window end it has then (a backfill job writes
  an episode once it has computed it through the latest data), and never changes. Only `live`
  episodes notify, once, at that first write (PLAN §7.1 step 7; E4 and E6 after ADR 0006 §11's
  hold). Updates never notify.
- **Persistence of trend-test rules** (B2, B4, B5, C2, N3: every rule whose condition includes a
  `trend` test; Architect, 2026-11-01). Their **condition** is the trend test in the rule's
  direction at its α plus the rule's own effect threshold where it has one. The run counts
  **calendar days** (Architect, 2026-11-01, after the #112 review): an episode **opens** at an
  evaluation when the condition held at every evaluation from the run's first evaluation day to
  this one, those days span **at least 21 calendar days** (first and last included), and there
  were **at least 6 evaluations**. A day without data neither counts nor breaks the run; a day
  with insufficient data breaks it. An open episode **ends** after **7 calendar days** in which
  every evaluation failed (at least one evaluation in them). The episode's window starts at the
  window start of the run's first evaluation, and its origin is computed when it opens. Why 21:
  with 14 the false-episode rate at φ 0.6 was 15 % of years, over the Architect's 10 % bound; 21
  gives 6 to 9 % with daily data (Consequences). For daily measurers the calendar count equals 21
  consecutive evaluations; for sparse ones it doesn't stretch the delay to 21 measures. Why 6
  evaluations (Architect, 2026-11-01): with 3, a weekly nerve cadence gave 13.0 % false years at
  φ 0.6; with 6 it gives 4.5 %. The minimum only bites on sparse metrics: 21 days of daily data
  hold 21 evaluations, and 21 days with data on 70 % of days hold fewer than 6 with a probability
  of about 2 · 10⁻⁵. The other rules keep the first-non-firing end above: their runs (C1, N2)
  are their persistence.
- Storage (the `findings` columns, the active-episode uniqueness) is the wiring issue's.

### 5. Confidence

`confidence` is one of **`low`, `medium`, `high`**: how well the data support the finding's
statement, never how likely a condition is. It is the **lowest** level among the criteria that
apply to the rule (§7 names them); our choice, auditable from `evidence` and `data_quality`:

| Criterion | Applies to | low | medium | high |
|---|---|---|---|---|
| **D** data | every rule with a policy window (the finding's own and its band's) | — | days with data in some window < 2 × its requirement's `min_days_with_data` | ≥ 2 × in every window |
| **T** trend test | rules on `trend` | n < 20 and `ar1_phi` > 0.5 (#87: the false-trend rate there is about 6 %, not 5 %) | p ≥ α/5 | p < α/5 |
| **R** run length | C1, N2 (routine) | — | the run is shorter than twice the rule's minimum | ≥ twice the minimum (C1: 6 days; N2: 4) |
| **J** jump flag | rules on weight or body composition | a trigger reading carries #37's advisory `jump` flag | — | — |

Device and system findings are stored `high`: they state exactly what Withings reported or what
state the connection is in. The UI shows "Withings' own results" instead of a confidence on
device findings, as the mock-ups do. `data_quality` is `summarise(...).as_dict()` of the rule's
window; Q1's parts without a series carry the state record they read (capability, last sync,
device row).

### 6. Severity

`severity` is a closed set that maps to the design's colours, constant per rule, and is **never a
diagnosis**. The clinical band a finding met, where there is one, is in `evidence`:

- **`urgent`** (the design's urgent red): E1 (a Withings AFib-positive result) and Q1's
  "connection needs reconnect". Nothing else.
- **`attention`** (orange): every other device, trend and experimental rule.
- **`info`** (no colour): Q1's no weigh-in, low battery and import incomplete.

### 7. The rules

"Window" is the finding's `window`; "ref" the reference day of §2. B2, B4, B5, N3 and C2 open and end
by §4's persistence rule. Every rule runs only on usable readings (#37), and returns
"insufficient data" (no finding) when a policy fails. **Every experimental rule (B1, B3, C1x,
and E4 through ADR 0006) is computed and shown only behind the owner-only flag on household
accounts** (PLAN §7.2, D9). Criterion J applies to B1 and B2 only: `weight` is the only metric
with a jump rule in `quality.METRICS`, so readings of 168, 169, 76, 170, 227 and 175 never carry
the flag.

**ECG (logic: ADR 0006 §11, which also decides which recordings count for E4 and E6)**

| Rule | Tier | Inputs | Method and parameters | Confidence | Severity |
|---|---|---|---|---|---|
| E1 | device | `heart list` `afib`, `afib_details` | ADR 0006 §11 | high | urgent |
| E2 | device | `afib_details` of the last 10 recordings | ADR 0006 §11 | high | attention |
| E3 | device | `afib_details` | ADR 0006 §11 | high | attention |
| E4 | experimental | 138, 136, 135 (one group) | ADR 0006 §11; bands there | high (a reported value) | attention |
| E5 | experimental | — | deferred by ADR 0006 §12: not in v1 | — | — |
| E6 | trend | `qtc_interval` (138), `qrs_duration` (135), daily value | daily value on *d* above the **finding threshold** at *d* (§2: k 2.5, the 90 days before *d*, BASELINE policy); window = *d* | D | attention |

**Body composition** (PLAN §7.2 B1 to B6)

| Rule | Tier | Inputs | Method and parameters | Threshold source | Confidence | Severity |
|---|---|---|---|---|---|---|
| B1 | experimental | `weight` (1), `extracellular_water` (168), daily values | At *d*: a day *d0* in *d* − 3 … *d* − 1 with a daily value and W(*d*) − W(*d0*) **more than 2.0 kg** (the largest gain; ties: the earliest *d0*), and ECW(*d*) above the ECW finding threshold at *d0* (ECW's BASELINE policy). Window *d0* … *d*; ref *d0*. The 7-day arm is not in v1 (§8) | ESC 2021 heart-failure guideline, Table 12 "Patient education and self-care" ("> 2 kg in 3 days"; McDonagh et al. 2021) | D (ECW band), J (weight readings) | attention |
| B2 | trend | `weight` | `trend` (AR(1), α 0.05) on the **182 days** ending *d*, TREND policy: `DECREASING` and `slope · 182 / intercept ≤ −0.05` (the Theil–Sen change over 6 months relative to the fitted level at the first point) | PLAN §7.2, own history | D, T, J | attention |
| B3 | experimental | 168 and 169 of the morning group (or of each group the median used: their median ratio) | ratio = ECW / (ECW + ICW) **more than 0.40** on *d*; window *d* | InBody's maker's (Biospace) classification, InBody's result-sheet interpretation; **device-specific and not a validated edema cut** (References) | D | attention |
| B4 | trend | `muscle_mass` (76) | `trend` on the 90 days ending *d*, TREND policy: `DECREASING` and `slope · 90 / intercept ≤ −0.02` | PLAN §7.2, own history | D, T | attention |
| B5 | trend | `visceral_fat` (170), `metabolic_age` (227), each its own subject | "rising" (§3) | own history | D, T | attention |
| B6 | trend | `segmental_muscle_mass` at positions 3/2 and 10/11 (keys `…:left_arm`, `…:right_arm`, `…:left_leg`, `…:right_leg`; duplicates keyed on `(grpid, position)`) | SI per group (§3); the median SI of the groups in the 90 days ending *d* **more than 0.10**, the days with a pair passing the BASELINE policy; subject arms or legs | PLAN §7.2, own data; SI from Robinson et al. 1987 | D | attention |

**Nerve health** (N4 is not in v1, §8)

| Rule | Tier | Inputs | Method and parameters | Confidence | Severity |
|---|---|---|---|---|---|
| N1 | trend | 167, 196, 229 | Not a finding: the nerve TREND policy (≥ 4 days with data spanning ≥ 28 days, `NERVE_TREND`) gates N2, N3 and N5, and below it the metric shows "insufficient data" | — | — |
| N2 | trend | `nerve_health_score` (167), `nerve_response_score` (196), `esc` (229), each its own subject; `attrib` 15 = guided | Band: the finding threshold over the #37 daily values that pass quality, guided and routine alike, of the 90 calendar days before the reference day (PRECEDING, `NERVE_BASELINE`); N1 on the 90 days ending *d*. **Guided**: a guided reading (`attrib` 15) on *d*, itself, below the threshold at *d*; window *d*. **Routine**: the daily values of the routine readings (`attrib` ≠ 15) on the last **2 consecutive days with data** (sparse data: not calendar days) both below the threshold at the first of them | D; R for routine | attention |
| N3 | trend | same keys | "significant 90/180-day decline" (§3); window: the 180 days if that window fired, else the 90 | D, T | attention |
| N5 | trend | 158/159, 197/198 (keys `nerve_foot_158` …, engineering bounds) | SI per group (§3); the median SI of the groups in the 90 days ending *d* **more than 0.20**, the days with a pair passing N1; otherwise not supported or insufficient data | D | attention |

**Heart and vessels**

| Rule | Tier | Inputs | Method and parameters | Threshold source | Confidence | Severity |
|---|---|---|---|---|---|---|
| C1 | trend | `heart_pulse` (11), daily value | A run (§3) ending at *d* where each day's value is **≥ median + 10 bpm** and **above the display high**, median and band over the 90 days before the run's first day (BASELINE policy). Wording: "10 bpm or more above your usual level, the middle of your typical range" (the mock-up's) | PLAN §7.2, own history | D, R | attention |
| C1x | experimental | same | A run (§3) where each day's value is **below 50 bpm** or each is **above 100 bpm** (one direction per episode) | sinus bradycardia < 50 bpm (Kusumoto et al. 2019, ACC/AHA/HRS 2018); sinus tachycardia > 100 bpm (Page et al. 2016, ACC/AHA/HRS 2015, confirmed through the ACC's published summary of it; the primary full text was not reviewed). Defined on the ECG sinus rate at rest; here applied to a reported pulse, one reason it stays experimental | R | attention |
| C2 | trend | `vascular_age` (155) | "rising" (§3) on y = VA − (day − 1970-01-01)/365.25: vascular age minus age rising. The birth year only shifts y by a constant, so it is not needed. The score-card part is not in v1 (§8) | own history | D, T | attention |

`C1x` is the `rule_id` of C1's experimental part, so the two tiers are separate findings. Its
> 100 bpm bound is **the one exception** to the rule that an experimental threshold without a
verified primary source is not in v1 (Architect, 2026-11-01): it is confirmed by the guideline's
co-publishing society's summary, and a rate above 100 bpm is the standard bound of sinus
tachycardia, as the bradycardia guideline (Kusumoto et al. 2019) also uses it.

**System** (tier `system`, roadmap Amendment 3)

| Q1 part | Subject | Condition | Confidence | Severity |
|---|---|---|---|---|
| No weigh-in for 14 days | user | the user has had a usable `weight` reading, the `measures` capability is `supported`, and `(today − last usable weight day).days ≥ 14`. The text states the data state; it never asks the user to weigh in (PLAN-operations §9.4) | high | info |
| Low battery | device | `devices.battery == "low"` (§3) on the latest `getdevice` | high | info |
| Connection needs reconnect | connection | the connection is `needs_reconnect` (PLAN §4.3) | high | urgent |
| Import incomplete | stream | §4.4's per-stream completeness says incomplete (as #25/#26 define it), not while importing | high | info |

### 8. Not in v1, and open

| Item | Why | Who settles it |
|---|---|---|
| C2's "above its score-card band" | the band is Withings' Health Improvement Score card from the decompiled app (findings §3): neither published nor the user's own history, and the abbreviated table leaves gaps between bands | the owner, with the HIS table |
| B1's "+2.5 kg in 7 days" | no published source found | the Architect, if a source is found (an amendment) |
| N4 (ESC bands < 40 / 40–60 µS) | no primary source: the candidate, Vinik, Nevoret and Casellini (2015), was checked in full text by the #112 review and has **no** < 40 / 40–60 µS bands (its diagnostic table gives a feet-ESC criterion of 77 µS), so it is not the source of PLAN's bands. Open too: whether Withings' type 229 is on the scale of Sudoscan's feet ESC that the studies used | the Architect, if a primary source is found; the 229 scale, by the owner's data |
| N2/N3's "monthly summary" | a view (the M6 nerve page, the M7 digest), not a rule | M6/M7 |
| Night HR as "resting HR" | needs a sleep normalisation M4 doesn't define | a later amendment |
| Health Improvement Score band table, Readiness, Body Path interval | PLAN §7.1 step 8; not finding rules, and their parameters are open | the owner (HIS bands); a later ADR |

## Alternatives

- **One band for display and findings.** At k 1.5 a single-value rule (E6) would fire on about
  7.5 % of unchanged values; at k 2.5 the displayed range would hide the design's band. Rejected.
- **k 3 for findings** (the Hampel identifier's usual cut): about 0.3 % one-sided under normal
  noise, but 1.7 % with heavy tails and less sensitive; Leys et al., after Miller (1991), call it
  very conservative.
- **C1 at "high + 10 bpm"** (the literal reading of "+10 above the personal range"): needs the
  mock-up's text changed. Rejected for the design's wording.
- **B2 and B4 by endpoint comparison** (today against six months ago): depends on two days.
  Theil–Sen uses every day and is robust to outliers.
- **B6 and N5 on the medians of each side**: pairs values from different groups; the per-group
  index compares two values measured together.
- **Hydration (77) as TBW**: its definition isn't documented; 168 + 169 is TBW by definition.
- **No persistence for trend tests** (first pass): 59 to 66 % of no-change years got a false
  trend episode per metric and direction. **14 consecutive evaluations**: 15 % at φ 0.6. A
  lower α instead of persistence: not chosen, the Architect kept α; persistence also
  removes one-day blips that a lower α would only make rarer.

## Consequences

- The M4 rules issue implements §2 to §7 and the test specification; the wiring issue adds the
  daily values' source groups, the segmental and per-foot keys, the episode storage and the
  evaluation-day scheduling; the M5 rules issue implements E1 to E4 and E6 on these rows.
- **Repeated testing.** A rule evaluated on every day with data tests overlapping windows again
  and again, so the share of no-change **years** with at least one false finding is far above a
  test's α. Synthetic check (the merged `trend`, AR(1), α 0.05, data on every day, a 90-day
  window evaluated daily for 365 days, no change, 300 years per row): about 2 % of evaluations
  call a rise, and without persistence **59 to 66 % of years have at least one false "rising"
  episode** per metric and direction. With the persistence rule of §4 (21 consecutive
  evaluations) it is **4.3 % at φ 0 and 6.3 % at φ 0.6**; 14 would have given 11 % and 15 %.
  The calendar-day form of §4 (a second run, 200 years per row, its own seeds) gives 4.5 % and
  8.5 % with daily data (the same rule, within the Monte Carlo error of about 2 points), 2.5 %
  and 7.5 % with data on 70 % of days. For a **weekly** nerve cadence (N3: the 90- or 180-day
  decline at α 0.025, `NERVE_TREND`) a minimum of 3 evaluations gave 2.5 % at φ 0 and 13.0 % at
  φ 0.6, above the 10 % bound, because 21 calendar days hold only 3 or 4 weekly evaluations; the
  minimum of 6 gives **0.8 % and 4.5 %** (400 years per row).
- **The price is delay, and it scales with how often the user measures.** A real decline of 5 %
  per 6 months (weight 80 kg, noise sd 0.5 kg: 8 noise sd per 182 days, our choice), on the
  90-day decline condition, opens an episode within 60 days of its start in 96 % of years at
  φ 0 and 39 % at φ 0.6 (100 % and 94 % without persistence). Median opening delay, calendar
  form: 48 / 66 days (φ 0 / 0.6) with daily data, 53 / 74 with 70 % of days, and 106 / 154 days
  for a weekly nerve cadence with the same signal and the 6-evaluation minimum (within 60 days:
  1.5 % / 4.5 %; within 180 days: 99.5 % / 64 %). **B2** finds no
  such change within 60 days at all, with or without persistence: its effect threshold is 5 %
  over its whole 182-day window, so it opens only once the change has run for most of six
  months, as its definition says. The tables are in the test specification ("Repeated
  testing").
- A trend finding's text stays descriptive ("rose over the last 90 days") and carries its
  confidence; an ended episode stays in the history.
- `baseline.py`'s `DEFAULT_K` stays 2.0; it is no longer anyone's band. Changing it is a code
  change for a later issue, not needed by this ADR.
- Every number here is a parameter with a version: a change is a dated amendment to this ADR and
  bumps the rule's `rule_version`.

## Open questions

**Blocked on the owner**

1. The Health Improvement Score band table, and with it C2's score-card part (§8).
2. Whether N4 returns once a primary source is verified, and whether a Body Scan's 229 value is
   on the cited studies' scale.

**Decided by the Architect (2026-11-01, #110).** Two bands (§2); C1 and its experimental part
(§7); the "not in v1" list (§8); confidence and severity (§5, §6); tier `system` by roadmap
Amendment 3; episodes and origin (§4); the persistence of trend-test rules (§4: 21 calendar days and
at least 6 evaluations to open, 7 to end; 21 chosen from {14, 21, 28} as the smallest under 10 %
false years at φ 0.6, and 6 evaluations because 3 left weekly nerve data at 13 %).
