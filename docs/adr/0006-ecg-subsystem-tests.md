# ADR 0006 §13: the synthetic test specification

Part of [ADR 0006](0006-ecg-subsystem.md), with the same status, split out to keep the ADR under 500
lines. "§n" means a section of ADR 0006, whose references this file cites. **Synthetic signals
only**: no real or public recordings. The generator and its options are in §13; strips are 30 s
long. Every threshold and tolerance here is our choice, and every seeded CI case must satisfy every
assertion; #40 implements them.

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

Ground truth applies §8's formulas to the generator's sinus intervals over the same span.

- **Clean** passed sinus-rhythm strips: RR error RMS ≤ 2 ms; HR ± 0.5 bpm; RMSSD and SDNN
  ± max(2 ms, 5 %); pNN50 within one pair (100 / pairs percentage points). Under the default
  variability no sinus interval is excluded.
- **Noisy and ectopic** conditions that pass: HR ± 1 bpm; RMSSD and SDNN ± max(3 ms, 10 %); pNN50
  within two pairs.
- **Ectopic beats**, anywhere in the strip, the first five intervals included: every premature
  interval and pause is excluded, and no sinus interval is.
- **Respiratory modulation** of the RR intervals, ± 12 % at 0.25 Hz: at HR ≥ 55 bpm no sinus
  interval after the first is excluded and the clean tolerances hold (the first can be, §8). At
  40 and 45 bpm, §8's limit: exactly the intervals more than 20 % from the last accepted one are
  excluded, and `hrv` is computed from the rest or null with its reasons; no tolerance against
  the unfiltered truth applies.
- **AF-like strips** have no HRV ground truth. On their fixed seeds: the gate as in the table, and
  `hrv` null with `too_many_excluded` (a reason code, never a display label). For every strip of
  any condition: more than 20 % excluded means `hrv` null with `too_many_excluded`.

## Simulation note (not CI assertions)

§8's exclusion rule applied to RR series alone, outside CI:

- respiratory modulation ± 12 % at 0.25 Hz, 2,000 phases per whole bpm from 40 to 180: at
  ≥ 55 bpm only the first interval is ever excluded, and only at 89–114 bpm; at 40 and 45 bpm up
  to 5 and 7 intervals are;
- ectopic beats as generated (60–70 % coupling, full compensatory pause, anywhere), 50,000 strips
  per background: no wrong exclusion with a sinus background of up to ± 5 %; 2 strips at ± 6 %;
- AF-like strips (lognormal RR, CV 0.25), 40,000 per rate: HRV computed in 1 strip at 100 bpm
  and in none at 120 or 150 bpm (8 at 80 bpm, outside the generator's range).
