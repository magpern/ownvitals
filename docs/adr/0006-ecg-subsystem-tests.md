# ADR 0006 §13: the synthetic test specification

Part of [ADR 0006](0006-ecg-subsystem.md), with the same status, split out to keep the ADR under 500
lines. "§n" means a section of ADR 0006, whose references this file cites and whose conventions
apply here: bounds are inclusive (Context). **Synthetic signals only**: no real or public
recordings. Strips are 30 s long. Every threshold and tolerance here is our choice, and every
seeded CI case must satisfy every assertion; #40 implements them, except those of the last
section, which names its issues.

## Generator

`backend/tests/ecgsynth.py`: **30-s strips** from the dynamical model of McSharry et al. (2003)
with the paper's PQRST parameters and heart-rate scaling; RR from its bimodal spectrum (LF 0.1 Hz,
HF 0.25 Hz; default RR SD about 2 %) or an explicit RR series. Output at 300 and 500 Hz, median R
amplitude A (default 1 mV), rounded to int µV like the API. Ground truth: R-event times and beat
types. Fixed seeds; files and data labelled synthetic. Options:

- AF-like: P amplitude 0, 0.05 mV f-waves at 5–7 Hz, independent lognormal RR with CV 0.25, mean
  HR 100–150 bpm;
- ectopic beats: a premature interval at 60–70 % of the preceding sinus interval and a full
  compensatory pause (the two sum to twice it), QRS twice as wide, inverted T; sinus background
  modulated by ± 2 % at 0.1 Hz and ± 2 % at 0.25 Hz;
- respiratory modulation of the RR intervals (not of instantaneous HR), a sinusoidal series alone;
- the noise models below.

## Noise

SNR = 10 · log₁₀((A_pp² / 8) / P_noise), with A_pp the median QRS peak-to-peak amplitude, in the
manner of the MIT-BIH noise stress test (Moody 1984), and P_noise over the noise's active time.
Models:

- white Gaussian; EMG-like (white, high-passed at 20 Hz);
- baseline wander (0.3 and 0.05 Hz sinusoids of amplitude A each);
- mains (50 or 60 Hz ± 0.2 Hz at 0.5 A, third harmonic 0.1 A below Nyquist);
- motion (1–3 s bursts band-passed 0.5–10 Hz, 50 ms tapers); electrode pops (a 2 A step decaying
  with τ = 0.3 s);
- dropout (zeros); clipping (plateaus ≥ 20 ms on the R-peaks).

## Filter responses (§5)

#40 tests the composed forward–backward magnitude at 300 and 500 Hz:

| Frequency | Requirement (expected value) |
|---|---|
| 1–30 Hz | between −1.0 and 0 dB (about −0.5 dB at 1 Hz; at 30 Hz −0.7 dB at 300 Hz, −0.8 dB at 500 Hz) |
| 0.5 Hz and 40 Hz | −6.0 ± 0.3 dB |
| 50 and 60 Hz, ± 0.1 Hz | ≤ −40 dB |
| 50 and 60 Hz, ± 0.5 Hz | ≤ −25 dB (about −28 to −30 dB at 49.5 Hz) |
| 0.05 Hz | ≤ −60 dB |

It also checks that a symmetric Gaussian pulse keeps its peak within 0.5 sample (zero phase) and
that sinusoids filtered by the real function, measured mid-signal, match the design within 1 dB.

## Beat matching and the gate

A detection matches a true beat within ±150 ms (ANSI/AAMI EC57). Se = TP / (TP + FN) and
+P = TP / (TP + FP) over the beats of accepted spans, **pooled** per condition and sampling rate
(EC57 gross statistics), plus a floor of 95 % on every accepted strip.

| Condition (each alone) | Gate | Pooled Se and +P |
|---|---|---|
| Clean, HR 40–180 bpm | passes 100 % | ≥ 99 % (expected 100 %) |
| White noise 12 dB; baseline wander; mains; A = 0.3 mV; inverted polarity | passes ≥ 95 % | ≥ 99 % |
| White or EMG-like noise 6 dB | may pass or fail | ≥ 98 % |
| AF-like rhythm; ectopic beats (≤ 2 per strip) | passes ≥ 95 % | ≥ 99 % |
| One motion burst ≤ 3 s at 0 dB at the start or end | passes ≥ 90 % | ≥ 99 % |
| Motion at 0 dB over ≥ 50 % of the strip | fails ≥ 95 % | — |
| Dropout (zeros) of 12 s at the start, in the middle and at the end of a 30-s strip | fails 100 % | — |
| Flat line, all zeros, clipping on every R-peak | fails 100 % | — |

## HR and HRV

Ground truth applies §8's formulas to the generator's sinus intervals over the same span. A pair
counts 100 / pairs percentage points of pNN50; the **band** is the number of true pairs whose
|ΔNN| lies within 50 ± 10 ms, which an R-peak timing error can move across the 50-ms threshold.

- **Clean** passed sinus-rhythm strips: RR error RMS ≤ 2 ms; HR ± 0.5 bpm; SDNN ± max(2.5 ms,
  5 %); RMSSD as in the table below; pNN50 within one pair plus the band. Under the default
  variability no sinus interval is excluded.
- **Noisy and ectopic** conditions that pass: HR ± 1 bpm; SDNN ± max(3.5 ms, 10 %); RMSSD as in
  the table below; pNN50 within two pairs plus the band.
- **Ectopic beats**, anywhere in the strip, the first five intervals included: every premature
  interval and pause is excluded, and no sinus interval is.
- **Respiratory modulation**, a sinusoidal RR series alone, ± 12 % at 0.25 Hz: at HR ≥ 55 bpm no
  sinus interval after the first is excluded and the clean tolerances hold (the first can be,
  §8). At 40 and 45 bpm, §8's limit: exactly the intervals more than 20 % from the last accepted
  one are excluded, and `hrv` is computed from the rest or null with its reasons; no tolerance
  against the unfiltered truth applies.
- **AF-like strips** have no HRV ground truth. On their fixed seeds: the gate as in the table, and
  `hrv` null with `too_many_excluded` (never a display label, §9). For every strip of any
  condition: more than 20 % excluded means `hrv` null with `too_many_excluded`.

RMSSD tolerance, by the generator's mean HR:

| HR | Clean and respiratory | Noisy and ectopic |
|---|---|---|
| 40 to under 60 bpm | ± 5.0 ms | ± 7.5 ms |
| 60 to under 100 bpm | ± 3.5 ms | ± 6.5 ms |
| 100 to under 140 bpm | ± 4.5 ms | ± 8.0 ms |
| 140 to 180 bpm | ± 5.5 ms | ± 7.5 ms |

The RMSSD and SDNN tolerances assume R-peak timing error no worse than independent Gaussian error
of SD 1.41 ms in the clean conditions (RR error RMS 2 ms) and 2.12 ms in the noisy and ectopic
ones. Each absolute value is the largest error in the simulation note for its conditions plus at
least 20 %, rounded up to 0.5 ms; each held in every simulated strip. Timing error of SD σ inflates
RMSSD by about √(RMSSD² + 6σ²) − RMSSD, so from 100 bpm, where the median true RMSSD of the
default variability is 5.1 ms (100–139 bpm) and 2.9 ms (140–180 bpm), the tolerance is of the
order of the value itself. There version 1 claims only this absolute bound, no relative accuracy.

## SQI bounds and components (§7)

At 300 and 500 Hz, on clean strips unless stated:

- A = 12 mV at 60 bpm fails with `amplitude_out_of_range`; A = 0.05 mV fails with `flat_line`.
- Constant RR at 25 bpm and at 230 bpm fails, with `implausible_rate` among the reasons.
- kSQI (Pearson kurtosis of the conditioned window): a clean strip at 60 bpm > 5 (Li 2008); white
  Gaussian noise, in the window from 10 to 20 s of a 30-s strip, within 3 ± 1.
- pSQI on 30-s sinusoids, every window: 10 Hz ≥ 0.99, 30 Hz ≤ 0.01. basSQI likewise: 0.3 Hz
  ≤ 0.01, 10 Hz ≥ 0.99.
- A DC offset: the same 1 mV sinusoids plus 1 mV of DC give the same basSQI bounds (0.3 Hz
  ≤ 0.01, 10 Hz ≥ 0.99), as §7 subtracts each window's mean before Welch.

## Seeds and other tests

Seeds: ≥ 20 per condition and rate in CI; a ≥ 200-seed `slow` sweep run by #40 and by every PR
that bumps a module version, its numbers in `docs/dev/ecg.md`. Also: the filter responses above; a
property test that a failed gate yields no downstream value; determinism and a golden output (a
golden change without a version bump fails); a guard against recording formats (WFDB, EDF,
SCP-ECG, HL7 aECG, DICOM waveform) and files over 256 KB under `backend/tests/ecg/`.

## Simulation note (not CI assertions)

Outside CI, on synthetic series; the first five items run §8's rules on RR series alone:

- respiratory modulation ± 12 % at 0.25 Hz, 2,000 phases per whole bpm from 40 to 180: at
  ≥ 55 bpm only the first interval is ever excluded, and only at 89–114 bpm; at 40 and 45 bpm up
  to 5 and 7 intervals are;
- ectopic beats as generated (60–70 % coupling, full compensatory pause, anywhere), 50,000 strips
  per background: no wrong exclusion with a sinus background of up to ± 5 %; 2 strips at ± 6 %;
- AF-like strips (lognormal RR, CV 0.25), 40,000 per rate: HRV computed in 1 strip at 100 bpm
  and in none at 120 or 150 bpm (8 at 80 bpm, outside the generator's range);
- pNN50 with Gaussian R-peak timing error of SD 1.41 ms (RR error RMS 2 ms, the clean bound):
  within one pair plus the band in all 42,300 strips of default variability (300 per bpm, 40 to
  180) and all 252,000 respiratory strips (2,000 phases per bpm, 55 to 180). At 1.5 times that
  error, two pairs plus the band hold in all default strips and miss in 2 respiratory ones (the
  noisy conditions use the default variability). Without the band, at SD 0.5 ms, 897 of 25,200
  respiratory strips miss "within one pair";
- RMSSD and SDNN with Gaussian R-peak timing error of SD 1.41 ms (clean) and 2.12 ms (noisy),
  1,000 strips per whole bpm from 40 to 180 per case (respiratory: 55 to 180), the RR series of
  the generator's spectrum and options; numpy 2.5.4, seeds as listed. Largest |RMSSD error| in ms:

  | Case (seed) | 40–59 bpm | 60–99 bpm | 100–139 bpm | 140–180 bpm |
  |---|---|---|---|---|
  | Default variability, 1.41 ms (11) | 3.84 | 2.83 | 3.37 | 4.17 |
  | Respiratory, 1.41 ms (21) | 2.06 | 2.19 | 0.89 | 0.92 |
  | Ectopic, 1.41 ms (13) | 3.92 | 2.15 | 2.31 | 2.86 |
  | Default variability, 2.12 ms (12) | 5.97 | 5.23 | 6.30 | 5.90 |
  | Ectopic, 2.12 ms (14) | 6.09 | 3.89 | 4.50 | 5.24 |

  The mean error under the default variability is +0.25, +0.56, +1.09 and +1.61 ms at 1.41 ms,
  and +0.54, +1.22, +2.18 and +3.00 ms at 2.12 ms. The earlier ± max(2 ms, 5 %) missed in 4 % of
  clean strips at 40 bpm, 4 % at 120, 8 % at 140, 20 % at 160 and 36 % at 180 bpm. The largest
  SDNN error is 2.03 ms at 1.41 ms (2 of 141,000 strips missed the earlier ± max(2 ms, 5 %)) and
  2.91 ms at 2.12 ms. The respiratory truth gives the same largest errors with or without §8's
  exclusion of the first interval, and no other exclusion differed from the truth;
- kSQI on a McSharry implementation: 10.7 at 40 bpm, 8.6 at 60 and 5.6 at 180 bpm; white noise
  2.6 to 3.7 in the middle window over 3,000 strips per rate. Edge windows of noise reach 14, as
  odd padding turns an edge sample into a step, hence the middle window;
- basSQI on 1 mV sinusoids with 1 mV of DC, every window at 300 and 500 Hz (scipy 1.18.1): at
  10 Hz 1.0000 with the window's mean subtracted (and with scipy's per-segment
  `detrend="constant"`), 0.3334 with neither; at 0.3 Hz 0.0000 in all three. pSQI on the
  conditioned window is unchanged by the offset (1.0000 at 10 Hz, 0.0000 at 30 Hz).

## The overlay, the read function and the notification hold (§11)

The M5 rules issue tests the overlay and the read function, and the M6/M7 notification issue the
hold, with synthetic findings and recordings:

- `ecg_gate_overlay` for E1 to E3, E4 and E6 against every gate state: pass; fail; and no gate
  result (no linked recording, a soft-deleted one, no `estimated`, an `input_fingerprint` that
  differs from the row's, a gate module (`input`, `filters`, `rpeaks`, `sqi`) that differs from
  the running one, `unsupported_sampling_rate`, `signal_missing`). An `hrv`-only difference keeps
  the result. Only E4 and E6 on a failure are hidden; every no-result state shows "not evaluated
  by ownvitals"; E1 to E3 are never hidden and show the gate outcome only with the owner-only flag.
- E6 with several trigger recordings: hidden with 1 of 2 and 2 of 3 failed; shown with 1 of 3
  failed and the others passed or without a result; "not evaluated" when none has a result.
- Only the trigger counts, in both directions. A failing trigger strip with a 10-strip baseline
  that all passed hides E6, as it hides E4 on the same strip; a passing trigger strip with 8 of
  its 10 baseline strips failed shows E6; a trigger without a gate result shows "not evaluated"
  whatever the baseline's results. The trigger is the measure groups listed in the evidence.
- The link is resolved at read time: a recording linked, deleted or recomputed after the finding
  changes the next read, with no write to the finding.
- The read function, for every output (viewer, API, export, doctor PDF, AI context): HR and HRV
  from a stale `estimated` (an `hrv`-only version difference, a different `input_fingerprint`)
  are left out; R-peak markers, SQI values and gate reasons are left out when §11's rule makes it
  absent and kept on an `hrv`-only difference. The guard test fails for a module outside the read
  function that reads findings or `estimated` for presentation, and passes the rules engine's
  writes, the `estimated` recompute and its sweep, and the account-delete and purge path.
- The hold, with a fake clock and the bound changed through its setting. An E4 or E6 notification
  is not sent while a trigger group can still be linked or a linked recording lacks a current
  `estimated`, and the first revision is younger than the bound. A pass sends it; a failure never
  does, even after a later recompute passes; a no-result `estimated` sends it marked "not
  evaluated". Just before the bound it is still held; at the bound it is sent marked "not
  evaluated". A link that appears during the hold counts. A new revision restarts the hold for
  that revision, the bound counted from the first; a withdrawn finding sends nothing. E1 to E3 are
  sent at once. A later failure recalls nothing.
- The hold waits only for trigger recordings: a baseline recording without a current `estimated`
  never holds it. With no linkable recording it ends at once and sends the notification marked
  "not evaluated", well before the bound: for each `ecg` capability state `unsupported`,
  `not_authorized` and `not_available`, and once an `ecg` sync that started after the trigger
  was stored completes without a match (but not after one that started before).
