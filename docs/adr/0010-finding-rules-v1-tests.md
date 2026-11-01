# ADR 0010: the synthetic test specification

Part of [ADR 0010](0010-finding-rules-v1.md), with the same status, split out to keep the ADR
under 500 lines. "§n" means a section of ADR 0010. **Synthetic data only**: hand-written daily
values and seeded generators labelled synthetic (`synthetic: true` or a `synthetic_` prefix),
never real readings. The M4 and M5 rules issues implement these cases with fake clocks (`today`
injected) and no database for the pure parts.

Each case states the data and the expected result. "Fires" means an active episode with the
stated window, tier, confidence and severity; "no finding" means none (and "insufficient data"
when a policy fails). Every threshold is tested at, just below and just above its boundary.

## Shared fixtures

- **Flat baseline.** 90 days with a daily value every day, values alternating so that median
  *m* and raw MAD *a* are known exactly (e.g. *m* ± *a* in equal numbers around *m*), so the
  display half-width is 1.5 · 1.4826 · *a* and the finding half-width 2.5 · 1.4826 · *a*.
- **Constant baseline**: 90 identical values, so both half-widths are the metric's minimum *w*.
- **Sparse baseline**: 9 days with data over 90 (below BASELINE's 10), and 10 days spanning 20
  days (below its 21-day span).

## Bands (§2)

1. The display band at *q* uses days *q* − 89 … *q* (`window_days=89`, TRAILING); a value on day
   *q* − 90 doesn't move it. The finding band at *q* uses *q* − 90 … *q* − 1 (PRECEDING); a value
   on *q* doesn't move it.
2. k: the finding threshold is exactly *m* + 2.5 · scale; a value equal to it does not fire
   ("above" is strict), one step above does.
3. Minimum half-width: a constant weight baseline of 80.0 kg gives display and finding bands of
   79.55 … 80.45 (*w* 0.45); a constant heart-pulse baseline of 61 gives 59 … 63 (*w* 2). A
   baseline with 1.4826 · MAD · 2.5 above *w* uses it for the finding band and *w* for the
   display band when 1.4826 · MAD · 1.5 is below *w*. `rolling_baseline` gets `min_scale = w / k`
   and the half-width equals `max(w, k · 1.4826 · MAD)` exactly. Metrics without a mock-up value
   use one display step.
4. Sparse baselines: no band is drawn, and every rule needing a band returns insufficient data.
5. A finding value is outside the displayed band, over a grid of *a*, `min_scale` and values.
6. `DEFAULT_K` is never used: every band call passes k explicitly (1.5 or 2.5), and a test fails
   when a rule or display path calls `rolling_baseline` without `k=`.

## Episodes and origin (§4)

1. `origin(window_end, today)`: `today − 6` → live; `today − 7` → backfill; `today + 1` → live.
2. An episode first written with a window end 10 days back is backfill and stays backfill when
   later firings extend it to today; one first written with today's end is live and stays live
   after 30 days.
3. Two consecutive firing evaluations update one active episode (window grows, `last_seen`
   moves, reference day and band unchanged); a non-firing evaluation ends it; the next firing
   opens a second episode. Insufficient data ends it too.
4. A recompute after a data revision that yields the same rule, subject and reference day keeps
   `first_seen` and origin; a different reference day is a new episode.
5. Only a live episode's first write notifies; updates never do; a backfill episode never does.
6. Per-reading day assignment: `today` follows the `source_tz` of the latest reading (a reading
   in Tokyo makes it the Tokyo date); fixed mode uses the fixed zone.

## Confidence and severity (§5, §6)

1. D: a window with exactly 2 × the minimum days is high; one fewer is medium.
2. T: n 19 with φ 0.51 is low; n 20 with φ 0.51 is not low on T; n 19 with φ 0.50 is not low on
   T; p just below α/5 is high on T, p at α/5 medium.
3. R: C1 runs of 3, 5 (medium) and 6 days (high); N2 routine runs of 2, 3 (medium) and 4 (high).
4. J: one trigger weight reading with `jump` makes B1 or B2 low whatever the rest; B3, B4, B5 and
   B6 never apply J (their metrics have no jump rule).
5. The lowest criterion wins (D high, T medium, R high → medium).
6. Severity: E1 and Q1 reconnect urgent; Q1's other parts info; every other rule attention. No
   rule maps a clinical band to a severity.

## Rules

**E6** (with ADR 0006's overlay tests): a QTc daily value just above the finding threshold fires
(window *d*); equal does not; the baseline below BASELINE is insufficient data; QTc and QRS are
separate subjects; the trigger provenance is the daily value's groups.

**Owner-only flag**: B1, B3 and C1x (and E4, by ADR 0006) are neither computed nor shown for an
account without the owner-only flag, nor ever for a non-household account; with the flag they
are. Trend, device and system rules don't depend on it.

**B1**: morning weights 80.0 on *d* − 3 and 82.1 on *d* with ECW above its threshold fires
(window *d* − 3 … *d*, ref *d* − 3); +2.0 kg exactly does not ("more than"); a gain from *d* − 4
does not; ECW at its threshold does not; ECW without a sufficient baseline is insufficient data;
the largest gain picks *d0*; a trigger weight reading with `jump` gives low (ECW readings never carry it); nothing about a 7-day gain
ever fires (not in v1).

**B2**: a weight series with a straight decline of 6 % over 182 days plus small noise fires; a
4 % decline does not; a 6 % decline whose `trend` is not significant does not; a 6 % rise does
not; 7 days with data (below TREND) is insufficient data; the window is 182 days. The ratio uses
`slope · 182 / intercept` exactly (a hand-computed five-point case).

**B3**: groups with ECW 15.0 and ICW 22.0 (ratio 0.4054) fire; 14.8 and 22.2 (exactly 0.4) do
not; a group without 169 gives no ratio; a user with no 169 ever is not available; ECW and ICW
from different groups are never paired.

**B4**: muscle −2.5 % over 90 days with a significant trend fires; −1.5 % does not; the boundary
−2 % exactly fires (≤).

**B5 and C2** ("rising"): a series stepping up by one index point at mid-window with daily data
fires; a flat series does not; visceral fat and metabolic age are separate subjects. C2: a
vascular age constant over 90 days does **not** rise (y falls with age), one rising 1 year per
month does; the result is the same with any birth year (none is passed).

**B6**: arms 3.00 / 2.70 kg in every group (SI 0.105) fire; 3.00 / 2.72 (SI 0.0979) do not;
values from different groups are never paired; arms and legs are separate subjects; a 90-day
median over a window with one outlier group stays below.

**N1–N3**: 3 nerve measures over 28 days is insufficient data (the tile says so) and no rule
runs; 4 over 28 days lets them run. N2 guided: one `attrib` 15 reading below the threshold fires
on its day even when a routine reading the same morning is the daily value; at the threshold it
does not. N2 routine: two consecutive measure days below fire, one does not, and a 10-day gap
between them still counts as consecutive measures. N3: a decline significant only on 180 days
fires with window 180; only on 90 days, window 90; p 0.03 on one window does not (α 0.025).
167, 196 and 229 are never mixed.

**N4**: never fires (not in v1), whatever the ESC value.

**N5**: pairs (158, 159) with SI 0.25 in every group fire; 0.18 do not; a user with only 158
gets "not supported", never a finding or "no difference"; swapping which member is left
changes nothing.

**C1**: 90 days of pulses around 61 (band 59–63), then 71, 72, 71 on three consecutive days
fires on the third (window the three days, ref the first, medium); 70 (median + 9) does not; a
missing middle day breaks the run; a noisy baseline whose display high is above median + 10
needs the value above that high; the run keeps the band of its first day while it grows.
**C1x**: 49, 48, 49 on three consecutive days fires (experimental, separate `rule_id`); 50 does
not ("below 50"); 101 three days fires, 100 does not; 49, 101, 49 is no run.

**Q1**: last usable weight 14 days before today fires, 13 does not; an `attrib` 1 reading
yesterday doesn't reset it; a user who never weighed in, or whose `measures` capability isn't
`supported`, gets nothing. Battery `low` fires per device, `medium` and a missing field don't, an
undocumented value doesn't. `needs_reconnect` fires urgent. Import: incomplete fires, importing
doesn't. Every Q1 finding has tier `system`.

## Persistence of trend-test rules (§4)

With the trend test replaced by a scripted condition (true or false per evaluation day), for each
of B2, B4, B5, N3 and C2. The run counts calendar days:

1. Daily data: true on days 1 … 20 opens nothing; true on day 21 opens an episode (21 calendar
   days, 21 evaluations), its window starting at day 1's window start, with the origin of day 21.
2. True on days 1 … 20, false on day 21, true from day 22: nothing opens until day 42.
3. Sparse data, the evaluation minimum: weekly evaluations on days 1, 8, 15, 22, 29 and 36, all
   true, open nothing on day 22 (22 days but 4 evaluations) or day 29 (5) and open on day 36 (the
   6th). Evaluations on days 1, 3, 5, 7, 9 and 21, all true, open on day 21 (21 days, 6
   evaluations); the same six ending on day 20 open nothing until a true evaluation on day 21 or
   later. Days without data neither count nor break the run.
4. Insufficient data on any evaluation day breaks the run; the next true evaluation starts a new
   one.
5. An open episode survives false evaluations on days *e* … *e* + 5 and ends on day *e* + 6 if
   every evaluation in those 7 calendar days failed; one true evaluation in them keeps it open.
   Seven calendar days with no evaluation at all don't end it. After it ends, a new episode needs
   a new run.
6. The effect threshold is part of the condition: B2 with a significant decline but a ratio of
   −0.049 on a run day is false that day and breaks the run.
7. C1, C1x, N2, E6, B1, B3, B6, N5 and Q1 are not affected: they open on their first firing and
   end on their first non-firing evaluation.

## Repeated testing (Consequences)

Synthetic checks, not CI assertions; the merged `trend` (AR(1) correction), data on every day,
seed 20261031 (with a per-cell offset). The noise is AR(1) with the stated φ.

**No change** (300 years per row): the 90-day "rising" condition (α 0.05) evaluated on each of
365 days. Percent of evaluations where it holds, and percent of years in which an episode opens
with an opening run of L evaluations (L 1 is no persistence):

| φ | per evaluation | L 1 | L 14 | **L 21** | L 28 |
|---|---|---|---|---|---|
| 0 | 2.0 % | 66.0 % | 11.0 % | **4.3 %** | 2.0 % |
| 0.6 | 2.2 % | 58.7 % | 15.0 % | **6.3 %** | 2.7 % |

The standard error of a year share is about 2.8 points at 60 % and 1.4 at 6 %.

**The calendar-day rule of §4** (21 calendar days, with the stated minimum of evaluations; 200 years per row,
seed 20261031 plus a cell offset; standard error about 2 points). Daily and 70 % rows: the
90-day "rising" condition (α 0.05, TREND policy, insufficient data breaking the run). Weekly row:
one measure every 7 days, N3's condition (`DECREASING` at α 0.025 on the 90 or the 180 days,
`NERVE_TREND`). Percent of no-change years with at least one episode, and for a linear decline
of 8 noise sd per 182 days (the weight case's 5 % per 6 months) the share opened within 60 and
180 days of its start and the median opening delay:

| Cadence | φ | false years | within 60 d | within 180 d | median delay |
|---|---|---|---|---|---|
| daily | 0 | 4.5 % | 97.5 % | 100 % | 48 days |
| daily | 0.6 | 8.5 % | 36.5 % | 100 % | 66 days |
| 70 % of days | 0 | 2.5 % | 82.5 % | 100 % | 53 days |
| 70 % of days | 0.6 | 7.5 % | 24.5 % | 99.5 % | 74 days |
| weekly (nerve, N3), at least 3 evaluations | 0 | 2.5 % | 7.5 % | 100 % | 89 days |
| weekly (nerve, N3), at least 3 evaluations | 0.6 | 13.0 % | 9.5 % | 74.5 % | 128 days |
| **weekly (nerve, N3), at least 6** (400 years) | 0 | **0.8 %** | 1.5 % | 99.5 % | 106 days |
| **weekly (nerve, N3), at least 6** (400 years) | 0.6 | **4.5 %** | 4.5 % | 64.0 % | 154 days |
| weekly (nerve, N3), at least 8 (400 years) | 0 | 0.2 % | 0 % | 96.2 % | 123 days |
| weekly (nerve, N3), at least 8 (400 years) | 0.6 | 1.5 % | 1.8 % | 47.5 % | 175 days |

The daily and 70 % rows ran with a minimum of 3; with 6 they are the same rule, since 21 days of
daily data hold 21 evaluations and 21 days at 70 % hold fewer than 6 with a probability of about 2 · 10⁻⁵. The daily rows repeat the L 21 rule (8.5 %
against 6.3 % is within two standard errors). With 3 evaluations the weekly row at φ 0.6 was above
10 %; 6, the Architect's minimum, brings it to 4.5 %, so 8 isn't needed.

**Power** (300 years per row; weight level 80 kg, noise marginal sd 0.5 kg, our choice): a linear
decline of 5 % per 182 days from day *s*; percent of years in which an episode opens on a day in
*s* … *s* + 60 from a run lying inside that span:

| Condition | φ | L 1 | L 14 | **L 21** | L 28 |
|---|---|---|---|---|---|
| 90-day decline (`DECREASING`) | 0 | 100 % | 100 % | **96.3 %** | 79.3 % |
| 90-day decline | 0.6 | 94.0 % | 63.3 % | **38.7 %** | 21.3 % |
| B2 (182 days, ratio ≤ −0.05), 200 years | 0 and 0.6 | 0 % | 0 % | 0 % | 0 % |

B2's zero is its definition: the Theil–Sen change over the whole 182-day window reaches 5 % only
once the decline has run for most of it, never within 60 days.

## References

- Dunn OJ. Multiple comparisons among means. J Am Stat Assoc 1961;56(293):52–64.
- Kusumoto FM, Schoenfeld MH, Barrett C, et al. 2018 ACC/AHA/HRS guideline on the evaluation and
  management of patients with bradycardia and cardiac conduction delay. Circulation
  2019;140(8):e382–e482 (sinus bradycardia: a sinus rate below 50 bpm).
- Leys C, Ley C, Klein O, Bernard P, Licata L. Detecting outliers: do not use standard deviation
  around the mean, use absolute deviation around the median. J Exp Soc Psychol
  2013;49(4):764–766 (median ± 2.5 MAD as the recommended default).
- McDonagh TA, Metra M, Adamo M, et al. 2021 ESC guidelines for the diagnosis and treatment of
  acute and chronic heart failure. Eur Heart J 2021;42(36):3599–3726, Table 12 "Patient education
  and self-care" (a sudden unexpected weight gain of more than 2 kg in 3 days).
- Miller J. Reaction time analysis with outlier exclusion: bias varies with sample size. Q J Exp
  Psychol A 1991;43(4):907–912 (the "very conservative" label for 3, as quoted by Leys et al.).
- Page RL, Joglar JA, Caldwell MA, et al. 2015 ACC/AHA/HRS guideline for the management of adult
  patients with supraventricular tachycardia. Circulation 2016;133(14):e506–e574 (sinus
  tachycardia: above 100 bpm). Confirmed through the American College of Cardiology's summary,
  "2015 ACC/AHA/HRS Guideline for the Management of Adult Patients With SVT: Ten Points to
  Remember", https://www.acc.org/latest-in-cardiology/ten-points-to-remember/2015/09/23/09/13/2015-acc-aha-hrs-guideline-for-the-management-of-adult-patients-with-svt;
  the primary full text was not reviewed (ADR §7, the one exception).
- Robinson RO, Herzog W, Nigg BM. Use of force platform variables to quantify the effects of
  chiropractic manipulation on gait symmetry. J Manipulative Physiol Ther 1987;10(4):172–176
  (the symmetry index, as commonly cited (R − L)/(½(R + L)) · 100 %; the ADR uses its magnitude
  as a fraction; the formula as commonly cited, the full text was not reviewed).
- Rousseeuw PJ, Croux C. Alternatives to the median absolute deviation. J Am Stat Assoc
  1993;88(424):1273–1283 (the scale 1.4826 · MAD).
- InBody (Biospace). "Result Sheet Interpretation", https://inbodybwa.com/result-sheet-interpretation:
  "A normal range of ECW Ratio is 0.360~0.390"; below 0.390 "Normal", 0.390–0.400 "Slightly
  Over", above 0.400 "Over"; used by multifrequency-BIA studies as an
  overhydration marker, not validated as an edema diagnosis, and specific to that device's
  method. Cited as the source of the number only.
- Checked for N4 and **not** its source (no < 40 / 40–60 µS bands; a feet-ESC criterion of 77 µS):
  Vinik AI, Nevoret ML, Casellini C. The new age of
  sudomotor function testing: a sensitive and specific biomarker for diagnosis, estimation of
  severity, monitoring progression, and regression in response to intervention. Front
  Endocrinol 2015;6:94.
- The methods' own sources (Mann, Kendall, Theil, Sen, Hamed and Rao, Bayley and Hammersley,
  Killick et al.) are in `docs/dev/analysis-methods.md`.
