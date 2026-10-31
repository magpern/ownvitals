# ADR 0006 §13: the synthetic test specification

Part of [ADR 0006](0006-ecg-subsystem.md), with the same status, split out to keep the ADR under 500
lines. "§n" means a section of ADR 0006, whose references this file cites. **Synthetic signals
only**: no real or public recordings. The generator and its options are in §13; strips are 30 s
long. Every threshold and tolerance here is our choice, and every seeded CI case must satisfy every
assertion; #40 implements them, except those of the last section, which names its issues.

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

- **Clean** passed sinus-rhythm strips: RR error RMS ≤ 2 ms; HR ± 0.5 bpm; RMSSD and SDNN
  ± max(2 ms, 5 %); pNN50 within one pair plus the band. Under the default variability no sinus
  interval is excluded.
- **Noisy and ectopic** conditions that pass: HR ± 1 bpm; RMSSD and SDNN ± max(3 ms, 10 %); pNN50
  within two pairs plus the band.
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

## SQI bounds and components (§7)

At 300 and 500 Hz, on clean strips unless stated:

- A = 12 mV at 60 bpm fails with `amplitude_out_of_range`; A = 0.05 mV fails with `flat_line`.
- Constant RR at 25 bpm and at 230 bpm fails, with `implausible_rate` among the reasons.
- kSQI (Pearson kurtosis of the conditioned window): a clean strip at 60 bpm > 5 (Li 2008); white
  Gaussian noise, in the window from 10 to 20 s of a 30-s strip, within 3 ± 1.
- pSQI on 30-s sinusoids, every window: 10 Hz ≥ 0.99, 30 Hz ≤ 0.01. basSQI likewise: 0.3 Hz
  ≤ 0.01, 10 Hz ≥ 0.99.

## Seeds and other tests

Seeds: ≥ 20 per condition and rate in CI; a ≥ 200-seed `slow` sweep run by #40 and by every PR
that bumps a module version, its numbers in `docs/dev/ecg.md`. Also: §5's filter responses; a
property test that a failed gate yields no downstream value; determinism and a golden output (a
golden change without a version bump fails); a guard against recording formats (WFDB, EDF,
SCP-ECG, HL7 aECG, DICOM waveform) and files over 256 KB under `backend/tests/ecg/`.

## Simulation note (not CI assertions)

Outside CI, on synthetic series; the first four items run §8's rules on RR series alone:

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
- kSQI on a McSharry implementation: 10.7 at 40 bpm, 8.6 at 60 and 5.6 at 180 bpm; white noise
  2.6 to 3.7 in the middle window over 3,000 strips per rate. Edge windows of noise reach 14, as
  odd padding turns an edge sample into a step, hence the middle window.

## The overlay and the notification hold (§11)

The M5 rules issue tests the overlay and the M6/M7 notification issue the hold, with synthetic
findings and recordings:

- `ecg_gate_overlay` for E1 to E3, E4 and E6 against every gate state: pass; fail; and no gate
  result (no linked recording, a soft-deleted one, no `estimated`, an `input_fingerprint` that
  differs from the row's, a gate module (`filters`, `rpeaks`, `sqi`) that differs from the
  running one, `unsupported_sampling_rate`, `signal_missing`). An `hrv`-only difference keeps the
  result. Only E4 and E6 on a failure are hidden; every no-result state shows "not evaluated by
  ownvitals"; E1 to E3 are never hidden and show the gate outcome only with the owner-only flag.
- E6 with several linked recordings: hidden with 1 of 2 and 2 of 3 failed; shown with 1 of 3
  failed and the others passed or without a result; "not evaluated" when none has a result.
- The link is resolved at read time: a recording linked, deleted or recomputed after the finding
  changes the next read, with no write to the finding.
- The hold, with a fake clock and the bound changed through its setting. An E4 or E6 notification
  is not sent while no recording is linked or a linked one lacks a current `estimated`, and the
  first revision is younger than the bound. A pass sends it; a failure never does, even after a
  later recompute passes; a no-result `estimated` sends it marked "not evaluated". Just before
  the bound it is still held; at the bound it is sent marked "not evaluated". A link that appears
  during the hold counts. A new revision restarts the hold for that revision, the bound counted
  from the first; a withdrawn finding sends nothing. E1 to E3 are sent at once. A later failure
  recalls nothing.
