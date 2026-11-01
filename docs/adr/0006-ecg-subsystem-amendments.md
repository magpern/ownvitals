# ADR 0006: amendments

The dated amendments of [ADR 0006](0006-ecg-subsystem.md) and of its test specification
([0006-ecg-subsystem-tests.md](0006-ecg-subsystem-tests.md)): the decisions, their evidence and
the numbers behind them. The two files keep the current rules, marked "amended <date>, #n" where
an amendment changed them; this file keeps the history. Numbers are from the synthetic generator
(`backend/tests/ecg/calibrate.py`; the tables are in [docs/dev/ecg.md](../dev/ecg.md)).

### 2026-10-31 (#88): follow-ups of the #46 review

Architect decisions on the non-blocking findings N41 to N45 of the #46 review (its Opus PASS at
`01d0fa3`), made in place above and in the test specification, marked "amended 2026-10-31, #88". The
test specification is part of this ADR, so its amendments are listed here too:

- §1, §15 (N44): every listed recording gets an `estimated`; a row kept with `signal` NULL gets one
  with `signal_missing`, which is no gate result. When that recording is the trigger's only linked
  one and no other trigger group can still be linked, a hold on it ends at once, with "not evaluated
  by ownvitals", instead of waiting out the 24-hour bound; otherwise §11 governs. #25 enqueues the
  recompute and #40's `input` step gives the reason. §15 lists a new row among the recompute's
  triggers, and the test specification's hold section gains the case.
- §2, §13 (N43, N45): A9 stays open until the M6/M7 notification hold exists, and until then OC5
  reports its late-link count as "not applicable"; A9 moved after A8. §13 says "a 95 % floor on
  every accepted strip", the test specification's wording, which governs.
- §11 and Consequences (N41): a daily value's trigger is the groups its value came from (the morning
  reading's group, or the readings the median used), not the day's fingerprint inputs.
- Test specification, "HR and HRV" (N42, the Architect's decision): the noisy RMSSD tolerance at 60
  to under 100 bpm is ± 7.0 ms, not ± 6.5 ms. That applies the 20 % margin rule as clean 60 to 99
  bpm already does; the evidence is in its simulation note. #40 follows whichever value is on `main`
  when it updates.
- References: moved verbatim to the end of the test specification, to keep this file within 500
  lines; the pointer and the test specification's introduction carry the marker. No decision
  changed.

### 2026-10-31 (#40): unreachable assertions and generator corrections

Architect decisions on assertions of this file that the specified algorithms could not meet,
found while implementing #40, with the evidence. The principle: **an accepted strip never carries
wrong values** (ADR 0006: no values, never wrong ones). A gate that refuses more often is the safe
direction, so a pass-rate target may change, but only by a recorded amendment with evidence; an
accuracy floor on accepted strips is never loosened, the algorithm is fixed instead. Numbers are
synthetic, from `backend/tests/ecg/calibrate.py`; the full tables are in `docs/dev/ecg.md`.

1. **AF-like gate pass (≥ 95 %) and the generator.** With the model's angular velocity 2π / RR_k
   per beat, a beat's R amplitude is proportional to RR_k (the integral of an event scales with
   1 / ω), so QRS shrink after a short RR, which real AF does not do. The generator now scales each
   event amplitude by ω_k / ω̄ (ω̄ = 2π · HR / 60), so a beat's deflections do not depend on its
   RR (Generator, amended in place). Before: 17 of 20 (300 Hz) and 18 of 20 (500 Hz) strips
   passed. After: 20 of 20 at both rates (pooled Se 99.68 % and 99.59 %, lowest strip 97.1 % and
   98.5 %). The threshold is unchanged. At 200 seeds the lowest strip is 95.08 % (300 Hz) and
   94.67 % (500 Hz), a tail strip reported in `docs/dev/ecg.md`.
2. **White noise at 12 dB, and baseline wander, below 50 bpm.** Elgendi stays as published:
   tuning it to this generator would overfit and weaken the independence bSQI needs. From 50 bpm
   the gate passes ≥ 95 % (white noise 97.8 % and 99.5 %, wander 96.2 % and 98.4 %, 185 seeds from
   50 to 180 bpm at 300 and 500 Hz); below 50 bpm it **may pass or fail**, and the accuracy floors
   (pooled ≥ 98 %, ≥ 95 % on every accepted strip) and the HR and HRV tolerances apply to the
   strips that pass (Beat matching, amended in place). Clean strips below 50 bpm are asserted
   apart: ≥ 95 % (40 of 40 at both rates). Evidence, 40 seeds from 40 to 49.9 bpm at 300 and 500
   Hz: white noise passes 4 and 22, wander 34 and 38. Causes: for white noise, Elgendi's threshold
   scales with the mean squared signal, which is small when QRS are sparse, so noise blocks wider
   than 97 ms pass and bSQI falls under 0.90; for wander, the clipping rule reads the smooth
   extremum of a 1 mV sinusoid (within 1 µV, the integer rounding, for about 47 ms) as a plateau
   at the strip's raw maximum when it falls between R waves, which at low HR it often does.
   **Known limitation**: below 50 bpm the gate may reject such strips; they show no values. The
   clipping rule is unchanged. A candidate for a later SQI version, if §14 V2 shows
   clipping-reason failures at low HR on real strips: a run counts only if the samples at both
   ends of it are entered and left by a step (a saturated trace has slope there, a wander
   extremum less than 2 µV per sample).
3. **Motion-burst +P floor: confirmed beats.** The per-strip floor is not loosened; the algorithm
   is fixed. The R-peaks that go downstream are the Pan–Tompkins detections that an Elgendi
   detection matches within ±50 ms (bSQI's own matching). An unmatched detection is dropped, and
   the intervals touching it are deleted like §8's exclusions, with reason `unconfirmed_beat`,
   counting toward the excluded fraction; the per-strip floors and the pooled Se and +P are
   measured on the beats that go downstream (§6, §8, amended in place). After: motion-burst
   strips keep the floors (with the rules of item 6, lowest +P 95.8 % and 96.4 %, pooled +P
   99.99 %), ectopic strips Se and +P 100 %, AF-like strips 20 of 20 as in item 1.
4. **Ectopic at 180 bpm.** A 60 % coupling at 180 bpm is a 197 ms premature interval, under the
   200 ms refractory period of the detector and not a realistic case. The premature interval is
   max(60–70 % of the preceding sinus interval, 250 ms), and the compensatory pause still brings
   the pair to twice the sinus interval (Generator, amended in place). The ± 1 bpm assertion
   stays. At 135 bpm and 500 Hz a strip has two premature intervals of 269.5 and 272.1 ms, just
   under the 272.7 ms (220 bpm) limit of §8; the R-peak timing error of the wide ectopic QRS (3.4
   and 3.9 ms) puts the detected intervals just over it, and HR then differs by 1.64 bpm from the
   truth. So, like the 20 % rule in item 5, **the HR assertion leaves out a strip with a true
   interval within ± 5 ms of 272.7 ms**; `calibrate.py` counts those strips, and `docs/dev/ecg.md`
   reports them. The strip is not re-seeded and the generator's 250 ms floor is unchanged.
5. **Respiratory modulation at 40 and 45 bpm: "exactly".** Several decisions sit within 0.1 % of
   the 20 % limit (at 45 bpm a series at 20.06 % in truth measures 19.97 %: a 0.7 ms offset of
   the fiducial). The assertion is: no decision contradicts the rule on the true intervals,
   except one within ± 0.5 % of the limit (HR and HRV, amended in place).
6. **Distorted beats and the verified span.** Accepted motion-burst strips must meet the HRV
   tolerances, or `hrv` must be null: HRV is not exempt. The rule that does it (§7 and §8, amended in
   place):
   - A confirmed beat is *distorted* when its Pearson correlation with the median beat of its
     window, over R ± 100 ms, is under `DISTORTED_R` = 0.85. The segment is fixed so that the
     neighbours of a premature beat and the beats of an irregular rhythm are not mistaken for
     distorted ones: with tSQI's segment (half the median RR) 38 % of AF-like beats, 6 % of
     ectopic strips' beats (the sinus beat before an ectopic beat among them) and 3.3 % of
     EMG-6-dB beats at 300 Hz fall under 0.90. tSQI itself is unchanged.
   - The intervals touching a distorted beat leave the NN intervals only (`distorted_beat`,
     counted in the excluded fraction); HR keeps them, because an ectopic beat is distorted and
     HR includes ectopic beats (a variant that dropped them from HR broke the ectopic HR truth).
   - A distorted beat within 5 s of an end of the span takes every beat between it and that end
     out; the span is measured on the beats left (under 20 s: `too_short`). A burst at an end of
     the strip adds, hides and displaces beats, and one count error in 30 s moves HR by about
     2 bpm at any rate, twice the tolerance, so the interval rule alone cannot make HR right:
     200 seeds of one burst of 1–3 s at the start or the end left 11 of 199 accepted strips at
     300 Hz and 11 of 199 at 500 Hz over the HR tolerance (SDNN, RMSSD and pNN50 were within
     theirs). With the trim: 0 of 199 and 1 of 199 (53 bpm, 1.11 times the HR tolerance, a tail
     case). Gate pass 99.5 % at both rates (≥ 90 % required).
   - **Why 0.85.** The correlation of correctly timed beats (n beats at 300 / 500 Hz, minimum and
     0.5th percentile at 300 / 500 Hz, beats under 0.85 and under 0.90):

     | Beats | n | Minimum | 0.5th pct | < 0.85 | < 0.90 |
     |---|---|---|---|---|---|
     | Clean, 40–180 bpm (200 seeds) | 10,943 / 10,946 | 0.967 / 0.952 | 0.980 / 0.992 | 0 / 0 | 0 / 0 |
     | Respiratory ± 12 % (200) | 11,770 / 11,770 | 0.937 / 0.927 | 0.961 / 0.967 | 0 / 0 | 0 / 0 |
     | Baseline wander (100) | 5,365 / 5,335 | 0.953 / 0.970 | 0.981 / 0.990 | 0 / 0 | 0 / 0 |
     | Mains (100) | 5,458 / 5,394 | 0.936 / 0.957 | 0.978 / 0.987 | 0 / 0 | 0 / 0 |
     | A = 0.3 mV; inverted (100 each) | 5,470 / 5,472 | 0.946 / 0.966 | 0.980 / 0.992 | 0 / 0 | 0 / 0 |
     | White noise 12 dB, from 50 bpm (100) | 5,642 / 5,716 | 0.906 / 0.953 | 0.945 / 0.969 | 0 / 0 | 0 / 0 |
     | EMG-like noise 6 dB (100) | 5,281 / 5,458 | 0.803 / 0.867 | 0.871 / 0.924 | 10 / 0 | 69 / 6 |
     | AF-like (200) | 12,398 / 12,395 | 0.567 / 0.659 | 0.778 / 0.795 | 387 / 278 | 1,151 / 967 |
     | Ectopic (200) | 10,930 / 10,941 | 0.426 / 0.450 | 0.534 / 0.536 | 298 / 300 | 356 / 344 |

     Ectopic beats themselves are at 0.43–0.84 (median 0.69). The sinus beat before an ectopic
     beat is at 0.85–0.92 at 300 Hz (1 of 242 under 0.85) and 0.88–0.95 at 500 Hz (none of 236):
     the generator scales a beat's waves with its following RR, so a premature interval pulls the
     T wave of the beat before it into the segment. The sinus beat after one is at 0.94 or more,
     every other sinus beat at 0.97 or more. So 0.85 is 0.10 under the clean minimum, not above
     the lowest sinus neighbour of an ectopic beat (0.90 would exclude sinus intervals next to
     ectopic beats, which the ectopic row of HR and HRV forbids), and flags 10 of 5,281 EMG-6-dB
     beats at 300 Hz. The beats a burst displaces by more than 10 ms, or invents, are at −0.09 to
     0.89 (median about 0.4; 40 of 43 at 300 Hz and 28 of 30 at 500 Hz under 0.90), and 364 of 575
     beats inside the bursts at 300 Hz are under 0.90.
7. **Decisions on choices of the implementation** (documented in `docs/dev/ecg.md`): the first
   and last 0.5 s of the strip are left out of the recorded kSQI and pSQI of the first and last
   window (odd padding turns an edge sample into a step: white-noise kSQI 2.6 to 3.7 instead of
   up to 14.6; they do not gate, and the gate is unchanged); Pan–Tompkins starts its signal level
   at 0.75 times the maximum of the first 2 s and treats the rate as irregular from 3 intervals
   (the paper fixes neither); the seeded white-noise, wander and clean rows are read per heart-rate
   band (`clean_lt50`, `white12_ge50`, `white12_lt50`, `wander_ge50`, `wander_lt50`, each its own
   ≥ 20 seeds in CI).
8. **Versions** (§15), as of 2026-11-01 before the review fixes of item 14: input 1.0.0, filters 1.0.0, rpeaks 2.0.0 (confirmed beats), sqi 3.0.0
   (confirmed beats, the verified span, the edge margin), hrv 4.0.0 (`unconfirmed_beat`,
   `distorted_beat`, `missed_beat`); the pipeline is `ecg-4.0.0`. A gate module changed, so every
   stored `estimated` is stale (§15); `hrv` and the pipeline are major bumps because HR and NN
   values change.

### 2026-11-01 (#40): missed beats and accepted tails of the 200-seed sweep

9. **Missed beats (rule (d), §8).** On 6 dB white noise at 173-174 bpm the gate accepted three
   strips (strips 190 and 191 at 300 Hz, 189 at 500 Hz) in which the detector missed one beat of
   86: the bridging interval left the NN intervals (`rr_deviation`) but stayed in HR, and one
   beat in 30 s is 1.9 bpm, 2.05 times the 1 bpm tolerance. The rule: on a strip whose other
   exclusions are at most 20 % of the intervals (so not on an irregular rhythm), an interval of
   at least 1.75 times the median of its up to eight neighbouring intervals (four each side,
   bridging intervals left out, at least three neighbours) leaves HR and NN with reason
   `missed_beat` and counts toward the excluded fraction. Distribution of the interval over the
   median of its neighbours, over 200 seeds at 300 and 500 Hz, accepted strips, intervals not
   already excluded by (0): clean 1.10 at most (below 50 bpm 1.10), respiratory ± 12 % 1.23,
   ectopic 1.45 (the compensatory pause), white 12 dB, EMG 6 dB, wander and mains 1.11 at most;
   AF-like up to 3.2 (the rule is off there: its other exclusions exceed 20 %). The rule flagged 3
   intervals in all, the three above, each with a true beat between its ends; no true interval
   was flagged on any row. After: white 6 dB HR margin 0.15 (300 Hz) and 0.07 (500 Hz).
10. **Tails accepted at 200 seeds.** (a) The Se floor of every accepted AF-like strip at 500 Hz
    is **94.5 %** (`tolerances.STRIP_SE_FLOOR_OVERRIDES`): the lowest of 200 seeds is 94.67 %
    (pooled Se 99.47 %, +P 100 %; 300 Hz 95.08 %, which keeps 95 %). Every other floor stays. (b)
    Burst strip 18 of 200 at 500 Hz (52.66 bpm; a burst at the start leaves one dropped beat and
    one deviating interval, HR 53.8) has HR 1.112 times the tolerance; `KNOWN_TAIL` names this
    seed, and every other strip of the row stays within 1.0.

### 2026-11-01 (#40): review fixes (Opus review of #114)

11. **No HR on an irregular rhythm (§8).** Item 10(a) accepted the AF-like sensitivity floor on the
    premise that the HR of those strips is within tolerance. That was never measured, and the
    premise is withdrawn: the review compared HR with the truth on every accepted strip,
    including the ones whose `hrv` is null (the tests had skipped them). On AF-like strips
    rule (d) is off, so a missed beat's bridging interval stays in HR: worst error 1.98 bpm at
    500 Hz and 1.69 bpm at 300 Hz over 100 seeds (strip 71 at 500 Hz, 135.9 bpm: 131.7 against
    133.68 true, three beats missed and bridged; strip 20: 111.3 against 113.0); at the CI's 20
    seeds 1.35 and 0.93 bpm. The "Worst HR 0.00" of the AF-like rows in `docs/dev/ecg.md` had
    measured nothing. Decision: a strip whose intervals excluded by (0), (a), (c) and (b) exceed
    20 % (the guard that switches rule (d) off) has `hr_bpm` null and `irregular_rhythm` as the
    last of its `hrv_reasons`; Withings' own HR stays the value shown. No unchecked value is
    shown. The 94.5 % Se floor stays as a detection metric only. The tests compare HR with the
    truth on every accepted strip that has one (`measure.hrv_error`), whether or not `hrv`
    exists, and assert null HR with that reason on every AF-like strip.
12. **Excluded fraction (§8).** `missed_beat` counts in it, as in item 9 and the code; the text
    said (0), (a), (c) or (b).
13. **Known limitation below 50 bpm (item 2), completed.** Such strips show no values; **E4/E6 are
    hidden when they are the trigger, which affects users with a resting rate under 50 bpm**; §14
    V2 reports the gate's failures by heart-rate band and reason; a later SQI version changes the
    secondary detector if real data show a material rate (candidate: the clipping rule of
    item 2).
14. **Matching and versions.** `rpeaks.matched_flags` takes the nearest *free* candidate within
    the tolerance, not only the two that bracket the beat (bSQI and the confirmation share it).
    `hrv` 5.0.0 (`irregular_rhythm`, HR null), `rpeaks` 3.0.0, `sqi` 4.0.0 (bSQI uses the
    matching); pipeline `ecg-5.0.0`. The ledger of the golden file holds this version only: 3.0.0
    and 4.0.0 never reached `main`. `record()` refuses to replace the current version's entry
    with other values.
