# ADR 0006: The ECG subsystem: data contract, quality gate, descriptive analysis and validation

Status: Accepted (2026-10-31, #39); amended 2026-10-31 and 2026-11-01 by #88 and #40 (see the amendments file). Assumptions A1 to
A9 stay open until the owner's probe and checks (§2); two questions are blocked on the owner (see
the end).

## Context

PLAN §7.3 makes the ECG subsystem its own specification, written before any interpretation code:
(1) verify the signal from real data, (2) preserve and display first, (3) a signal-quality gate,
(4) descriptive HR and HRV from validated R-peaks, (5) six-lead derivation only on proven inputs,
(6) interpretation in the experimental tier with its own release gate. PLAN §7.2 adds the tiers
(D9) and rules E1 to E6; PLAN §5 the storage (`ecg_recordings`: int32 µV, zstd; Withings' values
in `reported`, ours in `estimated` with the algorithm version) and provenance. M5 is done when it
is validated on the owner's history and the owner has reviewed the experimental tier (PLAN §11).

The public API gives one `signal[]` per recording (§1); which lead it is for Body Scan is unknown,
although Withings' app derives six leads from two (findings §2, §3; §5 question 1). Withings'
native libraries use IIR filters (0.3/0.5 Hz high-pass, 40 Hz low-pass, 50/60 Hz notch, at 250,
300 and 500 Hz) and QRS detection, and report RMSSD, SDNN, pNN50, PR, QRS, QT and QTc (findings
§3). We don't run them; we build a standard, cited chain around the same inputs.

Two rules bound everything: **no real health data** in the repository, CI or agent contexts, so
automated validation is synthetic (AGENTS.md); and **estimated values are never presented as
equivalent to Withings' certified measurements** (PLAN §7.3 step 4).

In this ADR "§n" is section n of the Decision below; PLAN and findings sections are named as such.
Values marked "our choice" are engineering choices with no literature source. **Bounds are
inclusive**: every "within", "±" and range here and in the test specification includes its ends.
Version 1 is **descriptive only**: #40 implements §4 to §9 and the CI part of §13 as a pure
library (`backend/src/ownvitals/ecg/`); interpretation (§12) is deferred.

## Decision

### 1. Data contract and storage (PLAN §7.3 steps 1–2, PLAN §5)

- **Fields** (findings §2). `heart list`: `signalid`, `afib`, `afib_details`, `heart_rate`, and the
  timestamp and time zone (→ `recorded_at` in UTC, `source_tz`; unnamed in the findings, so #25
  takes the names from the public spec). `heart get`: `signal[]` → `signal`, `sampling_frequency`
  → `sampling_hz`, `wearposition`, `model` (→ `model_id`). Every other field goes to `reported`
  (D4). Undocumented enum codes (the app also knows `NO_SIGNAL(3)`, `OTHER(4)`, findings §3) are
  stored as received and never given a meaning.
- **Leads.** The public `signal` is a flat array of integers: **one lead**. If a response ever
  carries several arrays (A5), each array is one lead.
- **Validation at ingest** (#25). Each lead is a non-empty array of JSON integers in the int32
  range, at most 1,000,000 samples (our choice); `sampling_frequency` is a positive integer up to
  2,000. Anything else is a `Permanent` failure with category `ecg_signal_out_of_contract`
  (PLAN §4.2): the list row is kept with `signal` NULL, and nothing is coerced, rounded or rescaled.
  Every listed recording gets an `estimated`, a row kept with `signal` NULL included (amended
  2026-10-31, #88): #25 enqueues its recompute (§15) when it stores the row, and #40's `input` step
  (§4) gives it the reason `signal_missing`, which is no gate result (§11). When that recording is
  the trigger's only linked one and no other trigger group can still be linked, a hold on it (§11)
  then ends at once, with "not evaluated by ownvitals", instead of waiting out the 24-hour bound;
  otherwise §11 governs.
- **Encoding, version 1** (#25). The uncompressed payload is `b"OVE1"`, `n_leads` (uint16 LE), a
  reserved uint16 (0), `n_samples` per lead (uint32 LE each), then the samples as int32 LE, lead
  after lead, in the order the API returned them. It is stored as one zstd frame (level 19,
  content checksum on); ingest decodes it again and compares with the API arrays before
  committing. Python 3.12 has no standard zstd module, so #25 adds the `zstandard` binding.
- **Payload hash** (#21): SHA-256 of the uncompressed payload, independent of the zstd level and
  library; PLAN §5's provenance needs one per input row, as `measure_groups.payload_hash`.
- **The original is never modified.** Only a re-fetch that finds changed source data rewrites
  `signal`; it bumps `data_revisions` for stream `ecg` and enqueues a recompute (§15). Filtered
  and derived signals are computed on read, never stored.

### 2. What only the owner can settle (PLAN §7.3 step 1)

The code runs on each assumption until the third column settles it; the Architect records each row
in `docs/status.md` ("Assumptions"). The probe prints shapes only (PLAN-operations §10.4). OC1 to
OC5 are **owner-run checks on the home server** (#47; OC1 and OC2 are visual comparisons) with
categorical output, never samples, values or timestamps. Agents never mark an assumption verified.

| # | Assumption until settled | Settled by | If it's wrong |
|---|---|---|---|
| A1 | Units and calibration: integers are µV with no further scale factor (public spec) | OC1: the owner opens one recording in our viewer and in Withings' app at 10 mm/mV and confirms the R amplitudes match | Amplitudes and the §7 amplitude limits are off. HR and HRV are unaffected unless the scale is so far off that the amplitude limits reject strips: then no values, never wrong ones |
| A2 | Lead identity unknown; shown as "lead unverified" | Probe: `wearposition` values. OC2: morphology matches the lead the Withings app labels | Labels only: analysis is lead- and polarity-agnostic (§6) |
| A3 | `sampling_frequency` is 300 or 500 Hz (the rates #40 validates) | Probe: sampling rate | Other rates are stored and displayed but not analysed (`unsupported_sampling_rate`); whether to add 250 Hz (findings §3) is decided after the probe |
| A4 | The stored signal may already be band-limited by the device (findings §3) | OC3: the spectrum above 40 Hz is ≥ 30 dB (our choice) below the 5–15 Hz band (device low-pass: yes/no); a 50/60 Hz line is present (yes/no) | Nothing in §5 to §9 (re-filtering at the same corners only steepens the roll-off); interval estimates (§12) wait for it |
| A5 | One array per recording (findings §3) | Probe: ECG array count and lengths. If > 1, OC4: equal length and rate, and R-peaks coincide within 20 ms (our choice) for ≥ 95 % of beats. R-peak times differ between leads, so it errs toward "not simultaneous", the safe direction | All arrays are stored; analysis uses the first; six-lead stays off (§10) |
| A6 | Strips last about 30 s | Probe: array length and sampling rate | Strips without a 20-s beat span never pass the gate (§7) |
| A7 | `getmeas` intervals (135–138) and AFib result (130) belong to the ECG with the same device and timestamp (findings §5 question 5) | Probe: counts of 135–138 against the ECG count. OC5: every ECG has a matching group (yes/no, count without) | E4 and E6 still run on the measures; only the link to a strip (viewer, §11 overlay) is lost, and their notifications go out "not evaluated by ownvitals" as soon as no recording can be linked (§11), without waiting for the 24-hour bound |
| A8 | `heart_rate` from `heart list` is the strip's mean rate | V3 (§14), which also validates our HR: a V3 failure needs a follow-up to tell which | Only V3's comparison |
| A9 | Withings lists an ECG in `heart list` by the time its 135–138 group is in `getmeas`, so an `ecg` sync that started after the group was stored and completed successfully sees it (§11's hold) | OC5 also counts the findings that got a link only after their hold had ended for want of one (the M6/M7 notification issue records how each hold ended, as a reason code; count only). A9 stays open until that hold exists; until then OC5 reports the count as "not applicable", since the end reasons don't exist yet (amended 2026-10-31, #88) | A late-listed strip's E4/E6 notification goes out "not evaluated by ownvitals"; if the strip then fails our gate, the overlay hides the finding, but the notification isn't recalled |

### 3. Preserve and display (PLAN §7.3 step 2)

- **Viewer** (canvas, PLAN §8): 25 mm/s and 10 mm/mV; 1 mm minor squares (40 ms, 0.1 mV), 5 mm major
  squares (200 ms, 0.5 mV); a 1 mV × 200 ms calibration pulse per row (Kligfield 2007). The viewer
  keeps the 25:10 ratio, zooms both axes together and labels the nominal scale; the gain is never
  auto-scaled (a labelled 5 mm/mV option is allowed) and traces are never clipped.
- **Default trace: the stored signal**, with only its median subtracted for vertical placement. An
  opt-in toggle shows the §5 chain, labelled "filtered 0.5–40 Hz". The lead label follows A2;
  several arrays are stacked rows (1..n leads, PLAN §12).
- **Withings' values as such**: "Withings classified this ECG as …" (PLAN §7.2) for documented
  `afib`/`afib_details` codes, "Withings code n" for undocumented ones; `heart_rate` and linked
  135–138 values labelled "Reported by Withings". A missing value reads "not reported", never 0
  or normal (PLAN §4.2).
- **PDF** (renderer: the M5 viewer/PDF issue's choice): A4 landscape at true scale when printed at
  100 %, rows of 10 s (250 mm), grid and calibration pulses, local recording time with its zone,
  device model, sampling rate, lead label and Withings' values with attribution. **No ownvitals
  estimates, nothing experimental**; it says the classification is Withings' and ownvitals is not
  a medical device. Our estimates appear only behind the experimental flag (§9).

### 4. Processing overview

```
signal (int32 µV) ─▶ input: checks, lead, float64 mV, window bounds
  ─▶ §5 conditioning: HP 0.5 Hz, notches 50 + 60 Hz, LP 40 Hz, all zero phase
  ─▶ §6 R-peaks: Pan–Tompkins (primary) and Elgendi (secondary), one fiducial rule
  ─▶ §7 SQI per 10-s window ─▶ gate on the longest beat span ─▶ fail: QualityRejected, stop
  ─▶ §8 RR ─▶ range check ─▶ 20 % rule ─▶ HR, RMSSD, SDNN, pNN50 ─▶ §9 `estimated`
```

Every step, a new one included, belongs to one versioned module (§15): `input` (the `signal_missing`
check, the supported rates 300 and 500 Hz (A3), the analysed lead (the first, A5), µV → mV and §7's
window bounds), `filters` (§5), `rpeaks` (§6), `sqi` (§7's indices, beat span and gate) and `hrv`
(§8). The first four, the **gate modules**, hold every step up to and including the gate, so any
change that can alter a gate result bumps one (§11). Each step is a pure function of the samples,
the sampling rate and the module versions (float64, no I/O, randomness or clock).

### 5. Conditioning filters

Subtract the median, then apply three filters, each designed per sampling rate as second-order
sections (`butter(..., fs=fs, output="sos")`; `iirnotch(f0, Q, fs=fs)` returns (b, a), converted
with `tf2sos`; the `tf` form is numerically unsafe at 0.5 Hz) and run forward–backward
(`sosfiltfilt`, zero phase, odd padding of min(2 s, n − 1) samples):

- **High-pass**: Butterworth order 2 at 0.5 Hz (−6 dB after the two passes; the composed −3 dB
  point is about 0.62 Hz). Zero-phase filtering allows a low cut-off up to 0.67 Hz without
  distorting the ST segment (Kligfield 2007); 0.5 Hz matches Withings' own (findings §3).
- **Notches** at 50 Hz **and** 60 Hz, Q = 30 (−3 dB width f₀/30 per pass), always both: they lie
  above the pass band, so no mains detector is needed and nothing in the band is lost.
- **Low-pass**: Butterworth order 4 at 40 Hz, the monitoring band Withings uses; R-peaks and HRV
  need nothing above it. Interval estimates would need a wider band (Kligfield 2007: ≥ 150 Hz for
  diagnostic ECG, here capped by Nyquist), set with §12.

#40 tests the composed response at 300 and 500 Hz against the test specification's table (§13).

### 6. R-peak detection

**Primary: Pan & Tompkins (1985) with its published rule set** (analysed by Hamilton & Tompkins
1986, whose alternative rules are not used), made independent of the sampling rate:

- band-pass 5–15 Hz (Butterworth order 2, zero phase) on the conditioned signal, five-point
  derivative, squaring, and a **centred** 150 ms moving-window integration (offline, so no delay
  separates an integrated peak from its QRS);
- candidate peaks at least 200 ms apart (refractory period). Two threshold sets, on the integrated
  signal (I1 = NPKI + 0.25 (SPKI − NPKI), I2 = I1 / 2) and on the band-passed signal (F1, F2
  likewise); a beat must exceed I1 and F1. Levels update as 0.125 · peak + 0.875 · level, or
  0.25 · peak + 0.75 · level for a beat found by search-back. The first 2 s train the levels;
- search-back with I2 and F2 when no beat follows within 166 % of the RR average of the last 8
  regular intervals (those within 92–116 % of the average); for an irregular rate I1 and F1 are
  halved, as published;
- T-wave discrimination: a candidate within 360 ms of the previous beat whose maximal slope is
  under half that of the previous QRS is a T wave.

**Secondary, for the agreement index only (§7): Elgendi (2013)**: band-pass 8–20 Hz (Butterworth,
zero phase; order 3 is our choice, the paper doesn't fix it), squaring, **centred** moving
averages over 97 ms (QRS) and 611 ms (beat). Blocks where the QRS average exceeds the beat
average + 0.08 × the mean squared signal, and that are at least 97 ms wide, are QRS complexes;
a block's **detection** is its sample of maximum absolute band-passed signal.

**Fiducial, the same for both**: the recording's polarity s is the sign of the largest deflection
of the median beat (±75 ms, our choice, around the first-pass detections); each R-peak is the
argmax of s·x within ±75 ms of its detection on the conditioned signal, refined by three-point
parabolic interpolation to a sub-sample position (the Task Force 1996 asks for ≥ 250 Hz or
interpolation). Beats whose ±75 ms window leaves the strip are dropped. The polarity rule makes
detection independent of lead identity and inversion (A2).

**Confirmed beats** (amended 2026-10-31, #40): the R-peaks that go downstream are the Pan–Tompkins
detections an Elgendi detection matches within ±50 ms (bSQI's own matching). An unmatched one is
dropped, and the interval across it is not an RR interval (§8, `unconfirmed_beat`).

Why: widely cited and fully specified (MIT-BIH: Pan–Tompkins 99.3 % of QRS; Elgendi Se 99.78 %,
+P 99.87 %), deterministic, numpy/scipy only, and of independent design, as bSQI needs.

### 7. Signal-quality index and the gate (PLAN §7.3 step 3)

Windows are 10 s long with a 5-s step; the last one is aligned to the strip's end. A window passes
when every row holds:

| Index | Definition | Pass | Source |
|---|---|---|---|
| bSQI | detector agreement: matched / (n₁ + n₂ − matched), matched within ±50 ms | ≥ 0.90 (our choice) | Li 2008; ±50 ms as in Behar 2013 |
| Rate | 60 · (n − 1) / (t_last − t_first) | 30–220 bpm | adapted from Orphanidou 2015 (40–180), widened for resting brady- and tachycardia |
| Gap | longest stretch without a beat, window edges included | ≤ 3.0 s | Orphanidou 2015 |
| tSQI | mean Pearson correlation of each beat (a median-RR window centred on R) with the window's **median** beat | ≥ 0.66 | adapted from Orphanidou 2015, which correlates with the mean beat |
| Amplitude | 99.5th − 0.5th percentile of the conditioned window | 0.1–10 mV | our choice (flat line, gross artefact) |
| Clipping | runs ≥ 20 ms of identical raw samples at the strip's raw minimum or maximum | none | our choice (saturation) |

Recorded per window but **not gating** in version 1, for §14 to judge: kSQI (kurtosis; clean
ECG > 5, Li 2008), pSQI (power 5–15 / 5–40 Hz), basSQI (1 − power 0–1 / 0–40 Hz of the
unfiltered window); Welch, 4-s Hann segments, 50 % overlap (Clifford 2012), on the window minus
its mean and with no further detrending (our choice: a DC offset doesn't count as baseline power).
The first and last window leave the strip's first and last 0.5 s out of kSQI and pSQI, where §5's
odd padding makes a step (amended 2026-10-31, #40).

- **Rhythm-agnostic by design.** Orphanidou's max/min RR ratio rule is left out and no index uses
  RR regularity: a gate that rejects irregular rhythm would bias any later rhythm analysis (E5)
  by construction (Behar 2013). #40 tests that AF-like and ectopic strips pass (§13).
- **Gate, measured on beats.** In each run of consecutive passing windows, the **beat span** runs
  from the first to the last R-peak detected in the run's windows. The recording passes when its
  longest beat span is ≥ 20 s (our choice); only the beats of that span go downstream. Measuring
  beats rather than window edges means overlapping windows never credit missing signal: a window
  holding more than 3 s of a dropout fails the gap rule, so no run crosses a dropout of ≥ 12 s;
  in a 30-s strip the ≤ 18 s left on either side can't reach 20 s, wherever the dropout sits.
- **Verified span** (amended 2026-10-31, #40). A confirmed beat (§6) is *distorted* when its Pearson
  correlation with the median beat of its window, over R ± 100 ms, is under 0.85 (a fixed QRS
  segment, so a premature beat's neighbours don't count; tSQI keeps its window). A distorted beat
  within 5 s of an end of the span takes every beat between it and that end out; the span is
  measured on the beats left (under 20 s: `too_short`). A burst of movement at an end of a strip
  adds, hides and displaces beats. Interior distorted beats are handled by §8. Derivation of the
  0.85: the amendments file.
- **Reasons** per window: `flat_line` (amplitude < 0.1 mV), `amplitude_out_of_range` (> 10 mV),
  `clipping`, `detector_disagreement`, `implausible_rate`, `beat_gap`, `low_template_correlation`.
  A failed recording carries `too_short` (no 20-s beat span) plus the union of its failing
  windows' reasons; before the gate, `signal_missing` or `unsupported_sampling_rate`.
- **A failing trace produces nothing downstream**: no R-peaks, HR, HRV or findings from our
  processing. #40 returns a typed `QualityRejected` that carries only the reasons and the
  per-window indices. Withings' own results stay visible (§11).
- Literature values are initial values that #40 checks against their sources; #40 confirms every
  value on §13's conditions, and only an amendment with a new SQI version changes one (§15).

### 8. RR intervals, artefact handling, HR and HRV (PLAN §7.3 step 4)

- **RR** = differences of consecutive refined R-peaks inside the beat span, in ms.
- **Exclusions**, in order (amended 2026-10-31 and 2026-11-01, #40): (0) the interval across a
  dropped beat (§6) → `unconfirmed_beat`; (a) outside 272.7–2,000 ms (30–220 bpm) →
  `rr_out_of_range`; (d) at least 1.75 times the median of its up to eight neighbouring intervals
  (four each side, (0) left out), on a strip whose other exclusions are at most 20 % → `missed_beat`
  (a beat the detector missed; a compensatory pause is 1.2–1.4 times); (c) touching a distorted
  beat (§7) → `distorted_beat`; (b) more than 20 % from the last accepted interval (the rule
  attributed to Malik 1989), the reference starting at the median of the span's first five
  intervals that are not (0), (d) or (c) → `rr_deviation`. Excluded intervals are **deleted, not
  interpolated** (Lippman 1994; Peltola 2012). Version 1 doesn't say whether an excluded interval
  is ectopic or an artefact: that would be interpretation (§12).
- **Limits of the 20 % rule.** With breathing at 0.25 Hz and HR below about 55 bpm, strong
  respiratory sinus arrhythmia changes successive sinus intervals by more than 20 %, so the rule
  drops real sinus intervals; at about 89–114 bpm it can drop the first interval, as the first
  five span most of a breath. While the excluded fraction stays ≤ 20 %, HRV is computed without
  them and reads low; beyond that, or under the minimums below, HRV is null ("insufficient").
  §13 tests this; V2 reports how often it happens on the owner's data.
- **HR** = 60,000 / mean of the intervals left after (0), (a) and (d): ectopic, distorted beats stay.
  **On an irregular rhythm HR is null** (amended 2026-11-01, #40): when the intervals excluded by
  (0), (a), (c) and (b) are over 20 % of all, rule (d) is off and a missed beat would corrupt the
  mean, so `hr_bpm` is null and `hrv_reasons` ends with `irregular_rhythm`. Withings' own HR is
  the value to show.
- **HRV** on the accepted (NN) intervals with the Task Force (1996) definitions and two
  conventions of ours (the common Kubios-style ones): SDNN = standard deviation with n − 1, which
  the Task Force leaves open; RMSSD = √(mean ΔNN²) over adjacent NN pairs only (both accepted,
  sharing a beat); pNN50 = 100 · (pairs with |ΔNN| > 50 ms) / **pairs**, where the Task Force
  divides NN50 by the number of NN intervals.
- HRV needs ≥ 15 NN intervals, ≥ 10 pairs and an excluded fraction (intervals excluded by (0), (a),
  (d), (c) or (b) over all intervals in the span) ≤ 20 %, all our choices; otherwise `hrv` is null
  with its reasons, `too_many_excluded` (the cause) before `insufficient_nn`. HR needs only the
  gate, except on an irregular rhythm.
- **Ultra-short-term** values (≤ 30 s): RMSSD holds up at 10–30 s, SDNN much less (Munoz 2015);
  neither matches 5-min or 24-h norms (Shaffer & Ginsberg 2017), so neither is compared with them.
- Rounding: HR 0.1 bpm, RMSSD and SDNN 0.1 ms, pNN50 0.1 percentage point.

### 9. The `estimated` record and how it is shown (PLAN §7.3 step 4)

`estimated` is canonical JSON (sorted keys, the rounding above):

- `algo_version` (the pipeline, e.g. `ecg-1.0.0`) and `modules` (each module's `ALGO_VERSION`:
  `input`, `filters`, `rpeaks`, `sqi`, `hrv`; §4);
- `input_fingerprint` (SHA-256 over `signalid`, `payload_hash` and `sampling_hz`) and
  `input_revision` (PLAN §5);
- `lead` (index analysed) and `sqi` (`result`, `reasons`, `span_s`, per-window component values);
- on a pass only: `rpeaks` (sample positions, 2 decimals), `excluded` (`[{i, reason}]`), `hr_bpm`,
  `n_rr`, and `hrv` (`rmssd_ms`, `sdnn_ms`, `pnn50_pct`, `n_nn`, `n_pairs`, `excluded_fraction`)
  or null with its reason. Nothing else: version 1 has no irregularity index or other
  interpretation field (§12).

How it is shown (findings never store our gate outcome; only §11's overlay brings it to them):

- **Only behind the owner-only flag**: HR, HRV, R-peak markers, SQI component values and gate
  reasons. PLAN §7.2 lists "independent estimates" in the experimental tier; showing them to
  anyone else is blocked on the owner (open question 1).
- **Only up-to-date estimates, in every output** (the viewer, API, JSON/CSV export, doctor PDF and
  AI context), read through §11's read function: HR and HRV only from an `estimated` that isn't
  stale (§15); R-peak markers, SQI values and gate reasons only from one that §11's rule doesn't
  make absent. The rest is left out until the recompute, never shown with a mark.
- **In every output** (viewer and the §11 consumers): labelled "Estimated by ownvitals from the
  raw signal; not a Withings measurement", in a panel or section of their own, never in a Withings
  slot or column, never filling a missing Withings value. An output that can't keep that label
  and separation leaves them out (the §3 PDF does). Never an input to device-tier rules or to
  Withings-sourced series (rule C1 uses measured HR). AI (M7) treats them as experimental.
- **No reason code (§7, §8) is ever a display label**, and neither `excluded` nor
  `excluded_fraction` is ever shown as an irregularity indicator (E5 is deferred, §12).
- Logs carry reason codes, counts and versions only, never samples or values (AGENTS.md).

### 10. Six-lead derivation (PLAN §7.3 step 5)

Off until A1, A2 and A5 are settled with two simultaneous arrays of equal calibration identified as
leads I and II. Then computed on read, never stored, shown as "derived": III = II − I, aVR =
−(I + II)/2, aVL = I − II/2, aVF = II − I/2 (Einthoven, Goldberger). Other leads need other
formulas and an amendment. Analysis stays on recorded leads.

### 11. Rules E1 to E6 (PLAN §7.2)

| Rule | Tier | Logic (Withings-reported values only) | Read-time gate overlay |
|---|---|---|---|
| E1 | device | `afib` = 1 or `afib_details` ∈ {1, 11, 12}: highlighted, strip shown, "share with a doctor"; evidence holds both raw codes | Never hidden |
| E2 | device | ≥ 3 of the user's last 10 recordings (by `recorded_at`, not deleted) with `afib_details` ∈ {2, 5}; with fewer than 10, the same count over those that exist | Never hidden |
| E3 | device | `afib_details` ∈ {6, 7} | Never hidden |
| E4 | experimental | QTc (138) > 450 ms (men) / 460 ms (women) and > 500 ms; PR (136) > 200 ms; QRS (135) ≥ 120 ms. Sex unknown: QTc uses only > 500 ms; PR and QRS as usual | Hidden on a gate failure of its trigger's linked (A7) recording |
| E5 | experimental | Our irregularity index against Withings' `afib` | Deferred (§12): nothing in version 1 |
| E6 | trend | QTc or QRS above the personal 90-day range, with M4's baseline and minimum-data policy (PLAN §7.1 steps 4–5) | Hidden when at least half of its trigger recordings with a gate result failed; recordings only in its baseline never count |

Architect's decisions (2026-10-31):

- **Findings hold Withings data only.** E1 to E6 are evaluated and stored from Withings-reported
  values and go stale by PLAN §5's normal rule (`data_revisions`, `input_fingerprint`); for E1 to
  E3, whose list fields no `payload_hash` covers, that rests on PLAN §5's revision counter over
  the Withings fields they read. No finding stores our gate outcome or `estimated.algo_version`.
- **Our gate is a read-time overlay.** A pure function, `ecg_gate_overlay`, decides whenever a
  finding is read, from the recordings linked to it:
  - **Trigger**: exactly the measure groups whose values produced the value (or values) that made
    the finding fire. The function that produces a value records them, and that provenance is
    carried into the finding's evidence; no other component re-derives the set. A single reading's
    trigger is its own group (E4's one group); a daily value's (`daily_values`, #37) is its morning
    reading's group when its method is `morning`, and every reading the median used when it is
    `median` (amended 2026-10-31, #88). A group that is also in the baseline counts as a trigger.
  - **Linked**: only the trigger's recordings, through the A7 link resolved at read time, never
    stored; a soft-deleted recording counts as no link. Recordings only in the 90-day baseline
    (the finding's other PLAN §5 inputs) are never linked and count toward neither the overlay
    nor the hold below; a gate on the baseline would be a rule of its own, by amendment.
  - **Current**: an `estimated` counts as absent when its `input_fingerprint` differs from the
    row's or its gate modules (`modules.input`, `filters`, `rpeaks`, `sqi`; §4) differ from the
    running ones (§15). A recording has a **gate result** (pass or fail) only from a current
    `estimated`; `unsupported_sampling_rate` and `signal_missing` give none.
  - E4, E6: of the trigger recordings with a gate result, hidden when at least half **failed**
    (PLAN §7.3 step 3: "a trace that fails the SQI produces no … findings"); an unlinked trigger
    group and a linked recording without a result both stay out of the count, and a failure never
    counts as a pass. Shown, marked "not evaluated by ownvitals", when none has a result.
  - E1 to E3: never hidden, since they relay Withings' classification and hiding a positive AFib
    result is the unsafe direction. Our gate outcome shows on them only behind the owner-only
    flag, so that it can't invite anyone to dismiss a Withings-reported result.
- **One read function.** Findings, and our estimates for every output (§9), are read only through
  one function, which loads the linked recordings, calls `ecg_gate_overlay` (in `ownvitals.ecg`,
  which the purity guard of `docs/dev/analysis.md` keeps free of database imports) and applies §9's
  up-to-date rule. A guard test fails if any other module reads findings or `estimated` for
  presentation; allowed by name: the rules engine's writes, the `estimated` recompute and its sweep
  (§15), the account-delete and purge path, and `ownvitals ecg validate` (#47), which reads the
  stored `estimated` to validate it (V5 compares it byte for byte). Consumers: the dashboard and
  API; notifications (below); the JSON/CSV export, with every finding and its overlay state (the
  user's complete data); the doctor PDF and the AI context (M7), which leave hidden findings out. A
  link that appears later, a deleted recording, a recomputed `estimated` and a new gate version are
  therefore current by construction, and writing `estimated` re-evaluates nothing (§15).
- **E4 and E6 notifications wait for the gate.** One is held while a trigger group can still be
  linked or a linked recording lacks a current `estimated`, for at most 24 hours (a setting) after
  the finding's first revision. A group can't be linked any more once the connection's `ecg`
  capability is `unsupported`, `not_authorized` or `not_available` (PLAN §4.2), or a sync of the
  `ecg` stream started after the group was stored has completed successfully without a match (A9; a
  failed or partial sync doesn't count); a finding with no linkable recording thus ends its hold at
  once. The overlay then decides once: hidden sends nothing, ever; shown sends it, marked "not
  evaluated by ownvitals" when there is no gate result. A new revision during the hold restarts it
  for that revision, the bound still counted from the first; a finding withdrawn during the hold
  sends nothing. E1 to E3 notify at once. A sent notification is never recalled.
- Every finding carries `rule_id`, `rule_version`, tier, `data_quality` and `origin`: an E1
  found while backfilling goes to the history view without a notification (PLAN §7.1 step 7).
  Only documented codes trigger a rule; a missing value never counts as negative.

### 12. Interpretation: deferred, experimental, with its own release gate (PLAN §7.3 step 6)

Version 1 is descriptive only (§4 to §9): no beat labels, no interval estimates, and the E5
irregularity index is neither computed nor stored (`estimated` has no field for it).
Interpretation (the E5 index and thresholds, ectopic-beat labels, PR, QRS, QT and QTc estimates)
is **deferred**: a later revision of this ADR specifies it before any code (parameters suited to
30-s strips, module and version, synthetic tests and tolerances); it then gets its own issue and
passes the release gate below.

Background only, not decisions: Dash et al. (2009) detect AF from normalised RMSSD, turning-point
ratio and Shannon entropy on trimmed 128-beat segments, so they need adapting to 20–40 intervals;
ectopy is usually found from timing (premature interval, compensatory pause) plus morphology;
intervals from wavelet delineation (Martínez 2004) on a wider band (§5) after A4, QTc by Bazett
and Fridericia.

**Release gate (owner-only).** All of: (1) the revision merged after a fresh Opus review (a
health-claims question); (2) its synthetic tests green in CI; (3) owner-run validation against
criteria written into the revision before the run; (4) the owner's go-ahead in the issue;
(5) visible only with the owner-only flag on household accounts, and never to a non-household user
before the M8 medical-claims review (PLAN-operations §9.3). E4 sits behind the same flag (D9).
Agents never mark the gate passed.

### 13. Validation in CI: synthetic signals only (#40)

**The test specification** is [0006-ecg-subsystem-tests.md](0006-ecg-subsystem-tests.md), part of
this ADR: the generator (`backend/tests/ecgsynth.py`, after McSharry et al. 2003), noise, beat
matching (ANSI/AAMI EC57; Se and +P pooled per condition and rate, a 95 % floor on every accepted
strip) (amended 2026-10-31, #88), gate conditions, §5's filter responses, HR, HRV and SQI
tolerances, seeds, a simulation note, and §11's overlay, read-function and hold tests. Its
thresholds are our choices; every seeded case must meet them. An unreachable threshold is reported
to the Architect; tests never loosen silently.

### 14. Owner-run validation (M5 done-when)

On the home server, by the owner, after the M2 real-data gate, with the owner-run command
`ownvitals ecg validate` (#47; Architect's decision, 2026-10-31): OC1 to OC5 (§2) and V1 to V5,
with aggregates and a pass/fail line per criterion. **Only the pass/fail lines go into the issue**
(the repository is public). The criteria are fixed now; the owner may change them before the run:

- **V1**: the gate passes ≥ 80 % (our choice) of the recordings Withings classified without
  marking them noisy or inconclusive (`afib_details` ∉ {−1, 2, 5, 13}).
- **V2** (report only): agreement table of our gate against Withings' noise code, the share of
  each HRV reason (for example `too_many_excluded` on slow, high-RSA strips, §8), and the gate's
  failures by heart-rate band (under 50 bpm) and reason.
- **V3**: |our HR − Withings' `heart_rate`| ≤ 5 bpm for ≥ 95 % of gate-passed recordings (both our
  choices), with a Bland–Altman summary (1986). V3 settles A8 **and** validates our HR, so a
  failure needs a follow-up to tell which; V4 checks our beats independently.
- **V4**: the owner inspects ≥ 20 random gate-passed strips with R-peak markers (≤ 1 % of beats
  wrong) and ≥ 10 rejected strips (rejection justified in ≥ 8); counts are our choices.
- **V5**: recomputing reproduces `estimated` byte for byte.

**Public research datasets** (PhysioNet and the like) are not used; any use is **blocked on the
owner** (open question 2).

### 15. Versioning and reproducibility (PLAN §5 provenance)

- Each module (§4) has an `ALGO_VERSION` (SemVer), and the pipeline version changes with any of
  them: a change that can alter any output bumps the major version, an added field the minor.
- **Staleness, a deliberate reading of PLAN §5.** PLAN §5 calls a derived row stale when its input
  revision or fingerprint differs. `estimated` derives from its own row only, so it is stale when
  its `algo_version` or `input_fingerprint` differs: the `ecg` revision counter is stream-wide and
  would mark every recording stale whenever any recording arrives, while the fingerprint covers this
  row's payload. `input_revision` is still stored. A backfill-lane job recomputes it
  (`origin=backfill`, no notifications), enqueued by a new row (§1) or a rewritten signal and by the
  sweep after a module version bump (amended 2026-10-31, #88). Until then §11's rule applies: an
  `estimated` counts as absent when its `input_fingerprint` differs from the row's or its gate
  modules (`modules.input`, `filters`, `rpeaks`, `sqi`) differ from the running ones, so an
  `hrv`-only bump keeps the gate results. Every output shows only up-to-date estimates (§9).
- **No revision bump and no re-evaluation, also deliberate.** `estimated` is derived output kept
  on the source row; bumping `data_revisions` on its write would make each analysis stale its own
  input. The revision bump (#21, #25) ignores updates that touch only `estimated`, and since no
  finding stores our gate outcome (§11), writing `estimated` re-evaluates no finding.
- Same payload and versions give the same `estimated`. numpy and scipy are pinned by `uv.lock`; an
  upgrade that changes the golden output is a version bump.
- A change to any parameter here is a dated amendment at the end of this ADR plus a version bump.

## Alternatives

- **Show only Withings' results**: safe, but no own analysis (D5) and no way to detect poor strips.
- **Learned detectors or quality classifiers**: they need real recordings, which the health-data
  rule keeps out. **ECG libraries** (NeuroKit2, BioSPPy, HeartPy): shifting defaults, extra
  dependencies; #40 is numpy/scipy only, and the published methods are short.
- **Interpolating excluded intervals or Kubios-style correction** (Lipponen & Tarvainen 2019):
  interpolation invents intervals, and adaptive correction needs about 90 beats of context.
- **One notch at a detected mains frequency** (needs a detector; notching both costs nothing);
  **causal filters** like the device's (phase distortion; offline we can stay zero-phase).
- **Rhythm rules in the SQI** (they would reject AF strips); **a gate on window edges** (overlapping
  windows would credit missing signal, §7).
- **Storing filtered signals or JSON arrays**: not the original, or about 5× the space (PLAN §5).
- **Storing our gate outcome in findings**, made stale through the version they record: every path
  that changes it (a late link, a deletion, a recompute, a version bump) would need its own
  re-evaluation trigger, and one missed path leaves a stale finding. The overlay (§11) has none.

## Consequences

- Version 1 is descriptive. #40 implements §4 to §9 and §13's CI part; #21 adds `payload_hash`; #25
  implements the rest of §1 (validation, zstd encoding, `zstandard`) and, with #21, a revision bump
  that ignores `estimated`-only updates (§15); the M5 viewer/PDF issue implements §3; the M5 rules
  issue implements §11 (E1 to E4, E6, `ecg_gate_overlay` and the one read function for findings and
  estimates, with its guard test), which every later consumer uses; the M6/M7 notification issue
  implements §11's hold of E4 and E6 notifications; #47 implements §14. E5 and the rest of §12 wait
  for the later revision, their own issue and the owner's gate.
- **Trigger provenance** (§11; amended 2026-10-31, #88): the M4 wiring issue makes each daily value
  carry its source groups' ids (#37's `DailyValue` has none today) through to the findings: the
  groups its value came from (the morning reading's group for `morning`, the readings the median
  used for `median`), not the day's fingerprint inputs. The M5 rules issue puts them in each
  finding's evidence; nothing re-derives them.
- **Known limitation** (amended 2026-10-31 and 2026-11-01, #40): below 50 bpm the gate may reject
  strips with white noise at 12 dB or baseline wander (causes: the amendments file); they show no
  values, and **E4/E6 are hidden when such strips are the trigger, which affects users with a
  resting rate under 50 bpm**. §14 V2 measures it by band and reason. A later SQI version changes
  the secondary detector if real data show a material rate (wander: a clipping run needs a step).
- `estimated` carries its versions and fingerprint, so a changed parameter recomputes history
  instead of mixing versions, and no output shows a superseded one meanwhile (§9); findings never
  depend on it, so they need no recompute.

## Open questions

**Blocked on the owner**

1. **Estimate visibility beyond the owner.** HR, HRV, R-peak markers and SQI component values are
   experimental (PLAN §7.2) and shown only behind the owner-only flag (§9). Showing them to anyone
   else, for example in the trend tier, is the owner's decision and a roadmap amendment.
2. **Public research datasets.** Not used. Any use is the owner's decision, under the datasets'
   licences and outside the repository, CI and agent contexts (§14).

**Decided by the Architect (2026-10-31).** Findings hold Withings data only; our gate is a
read-time overlay over a finding's trigger recordings, behind one read function, with a hold on
E4/E6 notifications; E4 with unknown sex uses only > 500 ms for QTc (§11). The owner-run
`ownvitals ecg validate` is #47 (§14); 250 Hz waits for the probe (A3); version 1 has no
interpretation, and the E5 index is deferred (§12).

## References

Listed at the end of [the test specification](0006-ecg-subsystem-tests.md#references) (moved, #88).
The dated amendments (#88, #40) and their evidence: [0006-ecg-subsystem-amendments.md](0006-ecg-subsystem-amendments.md).
