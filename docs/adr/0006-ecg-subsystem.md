# ADR 0006: The ECG subsystem: data contract, quality gate, descriptive analysis and validation

Status: Accepted (2026-10-31, #39). Assumptions A1 to A8 stay open until the owner's probe and
checks (§2); open questions: see the end.

## Context

PLAN §7.3 makes the ECG subsystem its own specification, written before any interpretation code:
(1) verify the signal from real data, (2) preserve and display first, (3) a signal-quality gate,
(4) descriptive HR and HRV from validated R-peaks, (5) six-lead derivation only on proven inputs,
(6) interpretation in the experimental tier with its own release gate. PLAN §7.2 adds the tiers
(D9) and the ECG rules E1 to E6. PLAN §5 fixes the storage (`ecg_recordings`: the signal as int32
µV, zstd; Withings' values in `reported`; ours in `estimated` with the algorithm version) and the
provenance rule. M5 is done when the subsystem is validated on the owner's history (owner-run) and
the owner has reviewed the experimental tier (PLAN §11).

What the public API gives (findings §2): `heart list` returns per recording `ecg.signalid`,
`afib`, `afib_details`, `heart_rate` and blood pressure; `heart get` returns `signal[]` in µV,
`sampling_frequency` (Hz), `wearposition` and `model`. Withings' app derives six leads from two
recorded ones, but the public API returns one `signal[]`, and which lead it is for Body Scan is
unknown (findings §3; §5 question 1). Withings' native libraries use IIR filters (0.3/0.5 Hz
high-pass, 40 Hz low-pass, 50/60 Hz notch, at 250, 300 and 500 Hz), QRS detection and a
line-noise detector, and report RMSSD, SDNN, pNN50, PR, QRS, QT and QTc (findings §3). We don't
run them; we build a standard, cited chain around the same inputs.

Two rules bound everything here. **No real health data** in the repository, CI or agent contexts
(AGENTS.md): all automated validation uses synthetic signals. And **estimated values are never
presented as equivalent to Withings' certified measurements** (PLAN §7.3 step 4).

In this ADR "§n" is section n of the Decision below; PLAN and findings sections are named as such.
#40 implements §4 to §9 and the CI part of §13 as a pure library (`backend/src/ownvitals/ecg/`).

## Decision

### 1. Data contract and storage (PLAN §7.3 steps 1–2, PLAN §5)

- **Fields.** From `heart list`: `signalid`; the timestamp and time zone (→ `recorded_at` in UTC,
  `source_tz`); `model` (→ `model_id`); `afib`, `afib_details`, `heart_rate`. From `heart get`:
  `signal[]` → `signal`, `sampling_frequency` → `sampling_hz`, `wearposition`. Every other field,
  known or not, goes to `reported` (D4). Undocumented enum codes (the app also knows
  `NO_SIGNAL(3)` and `OTHER(4)`, findings §3) are stored as received and never given a meaning.
- **Validation at ingest.** `signal[]` holds one or more non-empty arrays of JSON integers in the
  int32 range, at most 1,000,000 samples each; `sampling_frequency` is a positive integer up to
  2,000. Anything else is a `Permanent` failure with category `ecg_signal_out_of_contract`
  (PLAN §4.2): the list row is kept with `signal` NULL, and nothing is coerced, rounded or rescaled.
- **Encoding, version 1.** The uncompressed payload is `b"OVE1"`, `n_leads` (uint16 LE), a reserved
  uint16 (0), `n_samples` per lead (uint32 LE each), then the samples as int32 LE, lead after lead,
  in the order the API returned the arrays. It is stored as one zstd frame (level 19, content
  checksum on). Ingest decodes it again and compares with the API arrays before committing.
- **Payload hash.** `payload_hash` = SHA-256 of the uncompressed payload, independent of the zstd
  level and library. PLAN §5's provenance needs a payload hash per input row, as
  `measure_groups.payload_hash` has; the ingest issue adds the column.
- **The original is never modified.** Only a re-fetch that finds changed source data rewrites
  `signal`, and it bumps `data_revisions` for stream `ecg`. Filtered and derived signals are
  computed on read and never stored. Writing `estimated` is a derived write: it never bumps the
  revision (otherwise every analysis would make its own input stale).
- A 30-s strip at 500 Hz is 15,000 samples (60 KB uncompressed). Python 3.12 has no standard zstd
  module, so the ingest issue adds the `zstandard` binding; #40 needs no storage code.

### 2. What only the owner can settle (PLAN §7.3 step 1)

The code runs on the assumption in the second column until the item in the third settles it. The
Architect records each row in `docs/status.md` ("Assumptions"). The probe prints shapes only
(PLAN-operations §10.4). OC1 to OC5 are **owner-run checks on the home server** whose output is
categorical (yes/no, counts), never samples, values or timestamps. Agents never see the data and
never mark an assumption verified.

| # | Assumption until settled | Settled by | If it's wrong |
|---|---|---|---|
| A1 | Units and calibration: integers are µV with no further scale factor (public spec) | OC1: the owner opens one recording in our viewer and in Withings' app at 10 mm/mV and confirms the R amplitudes match | Amplitudes and the §7 amplitude limits are off; HR and HRV are not affected |
| A2 | Lead identity unknown; shown as "lead unverified" | Probe: `wearposition` values. OC2: morphology matches the lead the Withings app labels | Labels only: analysis is lead- and polarity-agnostic (§6) |
| A3 | `sampling_frequency` is 300 or 500 Hz (the rates #40 validates) | Probe: sampling rate | Other rates are stored and displayed but not analysed (`unsupported_sampling_rate`) until a test covers them |
| A4 | The stored signal may already be band-limited by the device (findings §3) | OC3: the spectrum above 40 Hz is ≥ 30 dB below the 5–15 Hz band (device low-pass: yes/no); a 50/60 Hz line is present (yes/no) | Nothing in §5 to §9 (re-filtering at the same corners only steepens the roll-off); interval estimates (§12) wait for it |
| A5 | One array per recording (findings §3) | Probe: ECG array count and lengths. If > 1, OC4: equal length and rate, and R-peaks coincide within 20 ms for ≥ 95 % of beats (yes/no) | All arrays are stored; analysis uses the first; six-lead stays off (§10) |
| A6 | Strips last about 30 s | Probe: array length and sampling rate | Strips under 20 s never pass the gate (§7) |
| A7 | `getmeas` intervals (135–138) and AFib result (130) belong to the ECG with the same device and timestamp (findings §5 question 5) | Probe: counts of 135–138 against the ECG count. OC5: every ECG has a matching group (yes/no, count without) | E4 and E6 still run on the measures; only the link to a strip (viewer, §11 gating) is lost |
| A8 | `heart_rate` from `heart list` is the strip's mean rate | Owner validation V3 (§14) | Only V3's comparison |

### 3. Preserve and display (PLAN §7.3 step 2)

- **Viewer** (canvas, PLAN §8): 25 mm/s and 10 mm/mV; 1 mm minor squares (40 ms, 0.1 mV), 5 mm major
  squares (200 ms, 0.5 mV); a 1 mV × 200 ms calibration pulse at the start of each row (Kligfield
  2007). Screen millimetres aren't physical, so the viewer keeps the 25:10 ratio, zooms both axes
  together and always labels the nominal scale. The gain is never auto-scaled (a labelled 5 mm/mV
  option is allowed) and traces are never clipped; rows may overlap.
- **Default trace: the stored signal**, with only its median subtracted for vertical placement. An
  opt-in toggle shows the §5 chain, labelled "filtered 0.5–40 Hz". The lead label follows A2;
  several arrays are stacked rows (1..n leads, PLAN §12).
- **Withings' values as such**: "Withings classified this ECG as …" (PLAN §7.2) for documented
  `afib`/`afib_details` codes, "Withings code n" for undocumented ones; `heart_rate` and linked
  135–138 values labelled "Reported by Withings". A missing value reads "not reported", never 0
  or normal (PLAN §4.2).
- **PDF**: A4 landscape at true scale when printed at 100 %: rows of 10 s (250 mm), grid and
  calibration pulses, recording time in local time with its zone, device model, sampling rate, lead
  label, and Withings' values with attribution. It holds **no ownvitals estimates and nothing
  experimental**, and says the classification is Withings' and ownvitals is not a medical device.
  The M5 viewer/PDF issue picks the renderer.
- Our estimates (§9) appear only behind the experimental flag, in a separate panel.

### 4. Processing overview

```
signal (int32 µV) ─▶ float64 mV
  ─▶ §5 conditioning: HP 0.5 Hz, notches 50 + 60 Hz, LP 40 Hz, all zero phase
  ─▶ §6 R-peaks: Pan–Tompkins (primary) and Elgendi (secondary), one fiducial rule
  ─▶ §7 SQI per 10-s window ─▶ gate on the longest accepted span ─▶ fail: QualityRejected, stop
  ─▶ §8 RR ─▶ range check ─▶ 20 % rule ─▶ HR, RMSSD, SDNN, pNN50 ─▶ §9 `estimated`
```

Every step is a pure function of the samples, the sampling rate and the module versions: float64,
no I/O, no randomness, no clock. Supported rates: 300 and 500 Hz (A3). Analysed lead: the first
array (A5).

### 5. Conditioning filters

Subtract the median, then apply three filters, each designed per sampling rate as second-order
sections (`scipy.signal.butter`/`iirnotch` with `fs=`, `output="sos"`; `tf` form is numerically
unsafe at 0.5 Hz) and run forward–backward (`sosfiltfilt`, zero phase, odd padding of
min(2 s, n − 1) samples):

- **High-pass**: Butterworth order 2 at 0.5 Hz (−6 dB after the two passes). Zero-phase filtering
  allows a low cut-off up to 0.67 Hz without distorting the ST segment (Kligfield 2007); 0.5 Hz
  matches Withings' own (findings §3).
- **Notches** at 50 Hz **and** 60 Hz, Q = 30 (−3 dB width f₀/30 per pass), always both: they lie
  above the pass band, so no mains detector is needed and nothing in the band is lost.
- **Low-pass**: Butterworth order 4 at 40 Hz, the monitoring band Withings uses; R-peaks and HRV
  need nothing above it. Interval estimates need a wider band (Kligfield 2007: ≥ 150 Hz for
  diagnostic ECG, here capped by Nyquist), set with §12.

#40 tests the composed forward–backward magnitude at 300 and 500 Hz:

| Frequency | Requirement (expected value) |
|---|---|
| 1–30 Hz | between −1.0 and 0 dB (−0.5 dB at 1 Hz, −0.85 dB at 30 Hz) |
| 0.5 Hz and 40 Hz | −6.0 ± 0.3 dB |
| 50 and 60 Hz, ± 0.1 Hz | ≤ −40 dB |
| 50 and 60 Hz, ± 0.5 Hz | ≤ −25 dB (about −28 dB at 49.5 Hz) |
| 0.05 Hz | ≤ −60 dB |

It also checks that a symmetric Gaussian pulse keeps its peak within 0.5 sample (zero phase) and
that sinusoids filtered by the real function, measured mid-signal, match the design within 1 dB.

### 6. R-peak detection

**Primary: Pan & Tompkins (1985)**, with the decision rules as published and analysed by Hamilton
& Tompkins (1986), made independent of the sampling rate:

- band-pass 5–15 Hz (Butterworth order 2, zero phase) on the conditioned signal, five-point
  derivative, squaring, moving-window integration over 150 ms;
- candidate peaks of the integrated signal at least 200 ms apart (refractory period); signal and
  noise levels updated as 0.125 · peak + 0.875 · level; threshold I1 = NPKI + 0.25 (SPKI − NPKI),
  I2 = I1 / 2; the first 2 s train the levels;
- search-back with I2 when no beat follows within 166 % of the RR average of the last 8 regular
  intervals (those within 92–116 % of the average);
- T-wave discrimination: a candidate within 360 ms of the previous beat whose maximal slope is
  under half that of the previous QRS is a T wave.

**Secondary, for the agreement index only (§7): Elgendi (2013)**: band-pass 8–20 Hz (Butterworth
order 3, zero phase), squaring, moving averages over 97 ms (QRS) and 611 ms (beat), threshold =
beat average + 0.08 × the mean squared signal; blocks at least 97 ms wide are QRS complexes.

**Fiducial, the same for both**: the recording's polarity s is the sign of the largest deflection
of the median beat (±75 ms around the first-pass detections); each R-peak is the argmax of s·x
within ±75 ms of its detection on the conditioned signal, refined by three-point parabolic
interpolation to a sub-sample position (the Task Force 1996 asks for ≥ 250 Hz or interpolation).
Beats whose ±75 ms window leaves the strip are dropped. The polarity rule makes detection
independent of lead identity and inversion (A2).

Why these two: the most cited simple detectors with published performance (Pan–Tompkins detected
99.3 % of QRS complexes on the MIT-BIH arrhythmia database; Elgendi Se 99.78 %, +P 99.87 %), fully
specified, deterministic, numpy/scipy only, and of independent design (different bands and
thresholds), which the agreement index needs.

### 7. Signal-quality index and the gate (PLAN §7.3 step 3)

Windows are 10 s long with a 5-s step; the last one is aligned to the strip's end. A window passes
when every row holds:

| Index | Definition | Pass | Source |
|---|---|---|---|
| bSQI | detector agreement: matched / (n₁ + n₂ − matched), matched within ±50 ms | ≥ 0.90 | Li 2008; ±50 ms as in Behar 2013 |
| Rate | 60 · (n − 1) / (t_last − t_first) | 30–220 bpm | Orphanidou 2015 (40–180), widened for resting brady- and tachycardia |
| Gap | longest stretch without a beat, window edges included | ≤ 3.0 s | Orphanidou 2015 |
| tSQI | mean Pearson correlation of each beat (a median-RR window centred on R) with the window's median beat | ≥ 0.66 | Orphanidou 2015 |
| Amplitude | 99.5th − 0.5th percentile of the conditioned window | 0.1–10 mV | flat line, gross artefact |
| Clipping | runs ≥ 20 ms of identical raw samples at the strip's raw minimum or maximum | none | saturation |

Recorded per window but **not gating** in version 1, for the owner validation to judge (§14):
kSQI (kurtosis; clean ECG > 5, Li 2008), pSQI (power 5–15 Hz / 5–40 Hz) and basSQI
(1 − power 0–1 Hz / 0–40 Hz of the unfiltered window), by Welch with 4-s Hann segments and 50 %
overlap (Clifford 2012).

- **Rhythm-agnostic by design.** Orphanidou's max/min RR ratio rule is left out and no index uses
  RR regularity: a gate that rejects irregular rhythm would make the irregularity index (E5) blind
  by construction (Behar 2013). #40 tests that AF-like and ectopic strips pass (§13).
- **Gate.** The recording passes when the longest run of consecutive passing windows spans ≥ 20 s.
  Only beats inside that span go downstream. Otherwise it fails with reason codes `too_short`,
  `flat_line`, `clipping`, `detector_disagreement`, `implausible_rate`, `beat_gap`,
  `low_template_correlation` or `amplitude_out_of_range`; before the gate, `signal_missing` and
  `unsupported_sampling_rate`.
- **A failing trace produces nothing downstream**: no R-peaks, HR, HRV, irregularity index,
  interval estimates or findings from our processing. #40 returns a typed `QualityRejected` that
  carries only the reasons and the per-window indices. Withings' own results stay visible (§11).
- The thresholds are initial values from the cited work. #40 confirms them on §13's conditions;
  only an amendment with a new SQI version changes them (§15).

### 8. RR intervals, artefact handling, HR and HRV (PLAN §7.3 step 4)

- **RR** = differences of consecutive refined R-peaks inside the accepted span, in ms.
- **Exclusions**, in order: (a) outside 272.7–2,000 ms (30–220 bpm) → `rr_out_of_range`; (b) more
  than 20 % from the last accepted interval (the rule attributed to Malik 1989), the reference
  starting at the median of the span's first five intervals → `rr_deviation`. Excluded intervals
  are **deleted, not interpolated** (Lippman 1994; Peltola 2012); 30-s strips are too short for
  adaptive correction such as Lipponen & Tarvainen (2019). Version 1 doesn't say whether an
  excluded interval is ectopic or an artefact: that is interpretation (§12).
- **HR** = 60,000 / mean of the intervals left after (a), ectopic beats included.
- **HRV** on the accepted (NN) intervals, per the Task Force (1996): SDNN = sample standard
  deviation (n − 1); RMSSD = √(mean ΔNN²) over adjacent NN pairs only (both accepted, sharing a
  beat); pNN50 = 100 · (pairs with |ΔNN| > 50 ms) / pairs.
- HRV needs ≥ 15 NN intervals, ≥ 10 pairs and an excluded fraction ≤ 20 %; otherwise `hrv` is null
  with `insufficient_nn` or `too_many_excluded`. HR needs only the gate.
- These are **ultra-short-term** values (≤ 30 s): RMSSD holds up reasonably at 10–30 s, SDNN much
  less (Munoz 2015), and neither is comparable with 5-min or 24-h norms (Shaffer & Ginsberg 2017).
  They are never compared with population norms.
- Rounding: HR 0.1 bpm, RMSSD and SDNN 0.1 ms, pNN50 0.1 percentage point.

### 9. The `estimated` record and how it is shown (PLAN §7.3 step 4)

`estimated` is canonical JSON (sorted keys, the rounding above):

- `algo_version` (the pipeline, e.g. `ecg-1.0.0`) and `modules` (each module's `ALGO_VERSION`:
  `filters`, `rpeaks`, `sqi`, `hrv`);
- `input_fingerprint` (SHA-256 over `signalid`, `payload_hash` and `sampling_hz`) and
  `input_revision` (PLAN §5);
- `lead` (index analysed) and `sqi` (`result`, `reasons`, `span_s`, per-window indices);
- on a pass only: `rpeaks` (sample positions, 2 decimals), `excluded` (`[{i, reason}]`), `hr_bpm`,
  `n_rr`, and `hrv` (`rmssd_ms`, `sdnn_ms`, `pnn50_pct`, `n_nn`, `n_pairs`, `excluded_fraction`)
  or null with its reason; `irregularity` (§12).

How it is shown:

- **Tier.** PLAN §7.2 lists "independent estimates" in the experimental tier, so HR, HRV, the SQI
  result and R-peak markers are visible only with the owner-only flag on household accounts
  (open question 1).
- Labelled "Estimated by ownvitals from the raw signal; not a Withings measurement", in a panel of
  their own: never in a Withings slot or column, never filling a missing Withings value, never in
  the PDF. Never an input to device-tier rules or to Withings-sourced series (rule C1's resting HR
  uses measured values). AI (M7) treats them as experimental (PLAN §7.4).
- Logs carry reason codes, counts and versions only, never samples or values (AGENTS.md).

### 10. Six-lead derivation (PLAN §7.3 step 5)

Off until A1, A2 and A5 are settled with two simultaneous arrays of equal calibration identified as
leads I and II. Then computed on read and never stored: III = II − I, aVR = −(I + II)/2,
aVL = I − II/2, aVF = II − I/2 (Einthoven, Goldberger), sample by sample, shown as "derived". If
the arrays are other leads, the formulas differ and need an amendment. Analysis stays on recorded
leads.

### 11. Rules E1 to E6 (PLAN §7.2)

| Rule | Tier | Logic and inputs | Our SQI gate |
|---|---|---|---|
| E1 | device | `afib` = 1 or `afib_details` ∈ {1, 11, 12}: highlighted, strip shown, "share with a doctor"; evidence holds both raw codes | Never applied |
| E2 | device | ≥ 3 of the user's last 10 recordings (by `recorded_at`, not deleted) with `afib_details` ∈ {2, 5}; with fewer than 10, the same count over those that exist | Never applied |
| E3 | device | `afib_details` ∈ {6, 7} | Never applied |
| E4 | experimental | Withings-reported QTc (138) > 450 ms (men) / 460 ms (women) and > 500 ms; PR (136) > 200 ms; QRS (135) ≥ 120 ms | A linked (A7) recording that failed is excluded |
| E5 | experimental | Our irregularity index (§12) against Withings' `afib` | Gate-passed traces only |
| E6 | trend | Withings-reported QTc or QRS above the personal 90-day range, with M4's baseline and minimum-data policy (PLAN §7.1 steps 4–5) | A linked recording that failed is excluded |

- E1 to E3 relay Withings' classification of the recording, so our gate never hides them: hiding a
  positive AFib result is the unsafe direction. E4 to E6 use values of the trace and follow
  "a trace that fails the SQI produces no … findings" (open question 2).
- Every finding carries `rule_id`, `rule_version`, tier, `data_quality` (with the SQI result when
  linked) and `origin`: an E1 found while backfilling goes to the history view without a
  notification (PLAN §7.1 step 7). Only documented codes trigger a rule; a missing value never
  counts as negative.

### 12. Interpretation: experimental tier and its release gate (PLAN §7.3 step 6)

Scope: the E5 irregularity index and its thresholds, ectopic-beat labels, interval estimates (PR,
QRS, QT, QTc). This ADR fixes:

- **Irregularity index, version 1**, computed and stored on gate-passed traces (visible only with
  the flag): over all intervals left after §8 (a) in the accepted span, normalised RMSSD
  (RMSSD / mean RR), turning-point ratio and Shannon entropy of the RR histogram (16 bins), as in
  Dash 2009. **E5 emits no findings until its thresholds pass the gate below**; Dash's published
  thresholds are the starting point, and the owner's validation sets the values, by amendment.
- **Ectopic labels and interval estimates** have intended methods only: timing plus morphology for
  ectopy (a premature interval with a compensatory pause, low template correlation), and wavelet
  delineation (Martínez 2004) on the wider band (§5), after A4, with QTc by Bazett and
  Fridericia. Their parameters, synthetic tests and tolerances come in an amendment to this ADR
  **before any code**.

**Release gate (owner-only).** All of: (1) the amendment merged after a fresh Opus review (a
health-claims question); (2) its synthetic tests green in CI; (3) owner-run validation against
criteria written into the amendment before the run; (4) the owner's go-ahead in the issue;
(5) visible only with the owner-only flag on household accounts, and never to a non-household user
before the M8 medical-claims review (PLAN-operations §9.3). E4 sits behind the same flag (D9).
Agents never mark the gate passed.

### 13. Validation in CI: synthetic signals only (#40)

**Generator** (`backend/tests/ecgsynth.py`): the dynamical model of McSharry et al. (2003) with the
paper's PQRST parameters and heart-rate scaling; RR from its bimodal spectrum (LF 0.1 Hz, HF
0.25 Hz) with configurable mean and SD, or an explicit RR series. Output at 300 and 500 Hz, scaled
so the median R amplitude is A (default 1 mV) and rounded to int µV like the API. Ground truth:
R-event times and beat types. Options: AF-like (P amplitude 0, 0.05 mV f-waves at 5–7 Hz,
independent RR with CV 0.25); ectopic beats (coupling 60–70 % of RR, compensatory pause, QRS
twice as wide, inverted T); the noise below. Fixed seeds; files and data labelled synthetic.

**Noise**: SNR = 10 · log₁₀((A_pp² / 8) / P_noise), A_pp the median QRS peak-to-peak amplitude, in
the manner of the MIT-BIH noise stress test (Moody 1984), P_noise over the noise's active time.
Models: white Gaussian; EMG-like (white, high-passed at 20 Hz); baseline wander (0.3 Hz and
0.05 Hz sinusoids of amplitude A each); mains (50 or 60 Hz ± 0.2 Hz at 0.5 A, plus its third
harmonic at 0.1 A below Nyquist); motion (1–3 s bursts of noise band-passed 0.5–10 Hz, 50 ms
tapers; electrode pops: a 2 A step decaying with τ = 0.3 s); dropout (zeros); clipping (plateaus
≥ 20 ms on the R-peaks).

**Matching**: a detection matches a true beat within ±150 ms (ANSI/AAMI EC57). Se = TP / (TP + FN)
and +P = TP / (TP + FP) over the beats of accepted spans.

| Condition (each alone) | Gate | Se and +P on accepted spans |
|---|---|---|
| Clean, HR 40–180 bpm | passes 100 % | ≥ 99 % (expected 100 %) |
| White noise 12 dB; baseline wander; mains; A = 0.3 mV; inverted polarity | passes ≥ 95 % | ≥ 99 % |
| White or EMG-like noise 6 dB | may pass or fail | ≥ 98 % |
| AF-like rhythm; ectopic beats (≤ 2 per strip) | passes ≥ 95 % | ≥ 99 % |
| One motion burst ≤ 3 s at 0 dB at the start or end | passes ≥ 90 % | ≥ 99 %, burst inside the span or not |
| Motion at 0 dB over ≥ 50 % of the strip | fails ≥ 95 % | — |
| Flat line, all zeros, dropout ≥ 12 s, clipping on every R-peak | fails 100 % | — |

On every passed clean strip: RR error RMS ≤ 2 ms; HR within ± 0.5 bpm; RMSSD and SDNN within
± max(2 ms, 5 %); pNN50 within one pair (100 / pairs percentage points). On noisy and ectopic
conditions that pass: HR ± 1 bpm, RMSSD and SDNN ± max(3 ms, 10 %), pNN50 within two pairs. Ground
truth applies the same §8 formulas to the generator's sinus intervals over the same span. Every
injected ectopic interval (the premature one and the pause) is excluded; no sinus interval is
under a sinusoidal respiratory modulation of ± 12 %, and at most 1 % are under the generator's
default variability.

Seeds: ≥ 20 per condition and rate in CI; a ≥ 200-seed sweep marked `slow`, run by #40 and by every
PR that bumps a module version, with its numbers in `docs/dev/ecg.md`. Other tests: the filter
responses (§5); a property test that a failed gate yields no downstream value; determinism and a
golden output, where a golden change without a version bump fails; a repository guard that fails
on recording formats (WFDB `.dat`/`.hea`/`.atr`, EDF, SCP-ECG, HL7 aECG, DICOM waveform) and on
any file over 256 KB under `backend/tests/ecg/`. If a threshold proves unreachable,
#40 reports the measured value to the Architect; tests never loosen silently.

### 14. Owner-run validation (M5 done-when)

On the home server, by the owner, after the M2 real-data gate. A CLI (open question 3) prints
aggregates and a pass/fail line per criterion. **Only the pass/fail lines go into the issue**: the
repository is public. The criteria are fixed now; the owner may change them before the run, not
after:

- **V1**: the gate passes ≥ 80 % of the recordings Withings classified without marking them noisy
  or inconclusive (`afib_details` ∉ {−1, 2, 5, 13}).
- **V2** (report only): agreement table of our gate against Withings' noise code.
- **V3**: |our HR − Withings' `heart_rate`| ≤ 5 bpm for ≥ 95 % of gate-passed recordings (A8),
  with a Bland–Altman summary (1986).
- **V4**: the owner inspects ≥ 20 random gate-passed strips with R-peak markers (≤ 1 % of beats
  wrong) and ≥ 10 rejected strips (rejection justified in ≥ 8).
- **V5**: recomputing reproduces `estimated` byte for byte.
- Plus OC1 to OC5 (§2).

**Public research datasets** (PhysioNet and the like) are not used. Using them is an owner
decision; even then they never enter the repository, CI or agent contexts (open question 5).

### 15. Versioning and reproducibility (PLAN §5 provenance)

- Each module has an `ALGO_VERSION` (SemVer); the pipeline version changes with any of them. A
  change that can alter any output for some input bumps the major version; an added field, the
  minor.
- `estimated` is stale when its `algo_version` or `input_fingerprint` differs from the current
  one; a recompute job runs in the backfill lane with `origin=backfill`, so it never notifies.
- Same payload and versions give the same `estimated`. numpy and scipy are pinned by `uv.lock`; an
  upgrade that changes the golden output is a version bump.
- A change to any parameter here is a dated amendment at the end of this ADR plus a version bump.

## Alternatives

- **Show only Withings' results**: safe, but no own analysis (D5) and no way to detect poor strips.
- **Learned detectors or quality classifiers**: they need real recordings to train and validate,
  which the health-data rule keeps out; opaque and harder to version.
- **ECG libraries (NeuroKit2, BioSPPy, HeartPy)**: their defaults change between releases and they
  pull in dependencies; #40 is numpy/scipy only, and the published methods are short.
- **A wavelet R-peak detector as primary**: no gain on resting 30-s strips; kept for delineation.
- **Interpolating excluded intervals or Kubios-style correction** (Lipponen & Tarvainen 2019):
  interpolation invents intervals, and adaptive correction needs about 90 beats of context.
- **One notch at a detected mains frequency**: needs a detector; notching both costs nothing.
- **Causal filters like the device's**: phase distortion; offline we can stay zero-phase.
- **Rhythm rules in the SQI** (max/min RR ratio): they would reject AF and blind E5.
- **Storing filtered signals or JSON arrays**: breaks "original unmodified" or costs about 5× the
  space of int32 + zstd (PLAN §5).

## Consequences

- #40 implements §4 to §9 and §13's CI part; the ingest issue implements §1 (`payload_hash`,
  `zstandard`); the M5 viewer/PDF issue implements §3; the rules issue implements §11 (E1 to E4
  and E6; E5 waits); §12 waits for its amendment and the owner's gate.
- The Architect adds A1 to A8 to `docs/status.md` with their settling items; OC1 to OC5 and V1 to V5
  need an owner-run tool (open question 3).
- Thresholds cited from the literature are checked against their sources by #40; a mismatch is
  reported, not silently changed.
- Every ECG output carries its versions and fingerprint, so a changed parameter recomputes
  history instead of mixing versions.

## Open questions

1. **Tier of descriptive HR and HRV (owner; before the M5 viewer).** PLAN §7.2 puts "independent
   estimates" in the experimental tier, so §9 shows them only behind the flag. Moving them to the
   trend tier (for example at the M5 experimental review) changes who sees them, so it is the
   owner's decision and a roadmap amendment, with the wording that labels them.
2. **Our SQI gate and E4/E6 (Architect).** §11 reads PLAN §7.3 step 3 as covering findings that use
   the trace's values (E4 to E6) but not the device-tier relay of Withings' result (E1 to E3).
3. **The owner-run tool (Architect).** Extend the probe (#29) with OC1 to OC5, or a separate
   `ownvitals ecg validate` for OC1 to OC5 and V1 to V5 in an M5 issue; categorical output either
   way.
4. **E4 when sex is unknown (Architect, with the E4 issue).** Apply only the > 500 ms band, or
   450 ms for everyone.
5. **Public research datasets (owner).** Not used unless the owner decides otherwise, then under
   their licences and outside the repository and agent contexts.
6. **Rates other than 300 and 500 Hz (Architect, after the probe).** 250 Hz (findings §3) needs its
   own row in §13's matrix.

## References

- ANSI/AAMI EC57:2012. Testing and reporting performance results of cardiac rhythm and ST segment
  measurement algorithms.
- Behar J, Oster J, Li Q, Clifford GD. ECG signal quality during arrhythmia and its application to
  false alarm reduction. IEEE Trans Biomed Eng 2013;60(6):1660–1666.
- Bland JM, Altman DG. Statistical methods for assessing agreement between two methods of clinical
  measurement. Lancet 1986;1(8476):307–310.
- Clifford GD, Behar J, Li Q, Rezek I. Signal quality indices and data fusion for determining
  clinical acceptability of electrocardiograms. Physiol Meas 2012;33(9):1419–1433.
- Dash S, Chon KH, Lu S, Raeder EA. Automatic real time detection of atrial fibrillation. Ann
  Biomed Eng 2009;37(9):1701–1709.
- Elgendi M. Fast QRS detection with an optimized knowledge-based method: evaluation on 11
  standard ECG databases. PLoS ONE 2013;8(9):e73557.
- Hamilton PS, Tompkins WJ. Quantitative investigation of QRS detection rules using the MIT/BIH
  arrhythmia database. IEEE Trans Biomed Eng 1986;33(12):1157–1165.
- Kligfield P, Gettes LS, Bailey JJ, et al. Recommendations for the standardization and
  interpretation of the electrocardiogram, part I. Circulation 2007;115(10):1306–1324.
- Li Q, Mark RG, Clifford GD. Robust heart rate estimation from multiple asynchronous noisy sources
  using signal quality indices and a Kalman filter. Physiol Meas 2008;29(1):15–32.
- Lippman N, Stein KM, Lerman BB. Comparison of methods for removal of ectopy in measurement of
  heart rate variability. Am J Physiol 1994;267(1 Pt 2):H411–H418.
- Lipponen JA, Tarvainen MP. A robust algorithm for heart rate variability time series artefact
  correction using novel beat classification. J Med Eng Technol 2019;43(3):173–181.
- Malik M, Farrell T, Cripps T, Camm AJ. Heart rate variability in relation to prognosis after
  myocardial infarction: selection of optimal processing techniques. Eur Heart J
  1989;10(12):1060–1074.
- Martínez JP, Almeida R, Olmos S, Rocha AP, Laguna P. A wavelet-based ECG delineator: evaluation
  on standard databases. IEEE Trans Biomed Eng 2004;51(4):570–581.
- McSharry PE, Clifford GD, Tarassenko L, Smith LA. A dynamical model for generating synthetic
  electrocardiogram signals. IEEE Trans Biomed Eng 2003;50(3):289–294.
- Moody GB, Muldrow WE, Mark RG. A noise stress test for arrhythmia detectors. Computers in
  Cardiology 1984;11:381–384.
- Munoz ML, van Roon A, Riese H, et al. Validity of (ultra-)short recordings for heart rate
  variability measurements. PLoS ONE 2015;10(9):e0138921.
- Orphanidou C, Bonnici T, Charlton P, Clifton D, Vallance D, Tarassenko L. Signal-quality indices
  for the electrocardiogram and photoplethysmogram: derivation and applications to wireless
  monitoring. IEEE J Biomed Health Inform 2015;19(3):832–838.
- Pan J, Tompkins WJ. A real-time QRS detection algorithm. IEEE Trans Biomed Eng
  1985;32(3):230–236.
- Peltola MA. Role of editing of R–R intervals in the analysis of heart rate variability. Front
  Physiol 2012;3:148.
- Shaffer F, Ginsberg JP. An overview of heart rate variability metrics and norms. Front Public
  Health 2017;5:258.
- Task Force of the ESC and NASPE. Heart rate variability: standards of measurement, physiological
  interpretation and clinical use. Circulation 1996;93(5):1043–1065.
