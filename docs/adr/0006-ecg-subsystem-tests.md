# ADR 0006 §13: the synthetic test specification

Part of [ADR 0006](0006-ecg-subsystem.md), with the same status, split out to keep the ADR under 500
lines. Its amendments, and the ADR's, are in
[0006-ecg-subsystem-amendments.md](0006-ecg-subsystem-amendments.md). "§n" means a
section of ADR 0006, whose conventions apply here: bounds are inclusive (Context). The sources both
files cite are listed under References at the end of this file (amended 2026-10-31, #88).
**Synthetic signals only**: no real or public recordings. Strips are 30 s long. Every threshold and
tolerance here is our choice, and every seeded CI case must satisfy every assertion; #40 implements
them, except those of the last section, which names its issues.

## Generator

`backend/tests/ecgsynth.py`: **30-s strips** from the dynamical model of McSharry et al. (2003)
with the paper's PQRST parameters and heart-rate scaling. Output at 300 and 500 Hz, median R
amplitude A (default 1 mV), rounded to int µV like the API. Ground truth: R-event times and beat
types. Fixed seeds; files and data labelled synthetic.

**RR series.** Every RR series is explicit: interval k is the series' value at the beat that
starts it, and the model's angular velocity is 2π / RR_k within that interval, so the R-events
fall at the series' beat times. The default series is ECGSYN's RR process (LF 0.1 Hz and HF
0.25 Hz, widths 0.01 Hz, LF/HF 0.5), generated at 1 Hz (256 samples), scaled to mean 60 / HR s
and SD 2 % of it, and read between samples by band-limited (Fourier-series) interpolation of
those samples, never linearly: linear interpolation at 1 Hz damps the 0.25 Hz component by about
19 %, and the RMSSD tolerances below depend on the realised RMSSD. Each event amplitude of a beat is
scaled by ω_k / ω̄ (ω̄ = 2π · HR / 60) so that its deflections do not depend on its RR (amended
2026-10-31, #40). Options:

- AF-like: P amplitude 0, 0.05 mV f-waves at 5–7 Hz, independent lognormal RR with CV 0.25, mean
  HR 100–150 bpm;
- ectopic beats: a premature interval at max(60–70 % of the preceding sinus interval, 250 ms) and a
  full compensatory pause (the two sum to twice it), QRS twice as wide, inverted T; sinus background
  modulated by ± 2 % at 0.1 Hz and ± 2 % at 0.25 Hz (the 250 ms: amended 2026-10-31, #40);
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
| Clean, HR 40–49.9 bpm, asserted apart (amended 2026-10-31, #40) | passes ≥ 95 % | ≥ 99 % |
| White noise 12 dB; baseline wander, HR 50–180 bpm (amended 2026-10-31, #40) | passes ≥ 95 % | ≥ 99 % |
| White noise 12 dB; baseline wander, HR 40–49.9 bpm (amended 2026-10-31, #40) | may pass or fail | ≥ 98 % |
| Mains; A = 0.3 mV; inverted polarity | passes ≥ 95 % | ≥ 99 % |
| White or EMG-like noise 6 dB | may pass or fail | ≥ 98 % |
| AF-like rhythm; ectopic beats (≤ 2 per strip) | passes ≥ 95 % | ≥ 99 % |
| One motion burst ≤ 3 s at 0 dB at the start or end | passes ≥ 90 % | ≥ 99 % |
| Motion at 0 dB over ≥ 50 % of the strip | fails ≥ 95 % | — |
| Dropout (zeros) of 12 s at the start, in the middle and at the end of a 30-s strip | fails 100 % | — |
| Flat line, all zeros, clipping on every R-peak | fails 100 % | — |

Se, +P and the 95 % floor are measured on the beats that go downstream: the confirmed beats of the
verified span (§6, §7; amended 2026-10-31, #40).

## HR and HRV

Ground truth applies §8's formulas to the generator's sinus intervals over the same span. A pair
counts 100 / pairs percentage points of pNN50; the **band** is the number of true pairs whose
|ΔNN| lies within 50 ± 10 ms, which an R-peak timing error can move across the 50-ms threshold.

- **Clean** passed sinus-rhythm strips: RR error RMS ≤ 2 ms; HR ± 0.5 bpm; SDNN ± max(3.0 ms,
  5 %); RMSSD as in the table below; pNN50 within one pair plus the band. Under the default
  variability no sinus interval is excluded.
- **Noisy and ectopic** conditions that pass: HR ± 1 bpm; SDNN ± max(4.5 ms, 10 %); RMSSD as in
  the table below; pNN50 within two pairs plus the band.
- **Ectopic beats**, anywhere in the strip, the first five intervals included: every premature
  interval and pause is excluded, and no sinus interval is.
- **Respiratory modulation**, a sinusoidal RR series alone, ± 12 % at 0.25 Hz: at HR ≥ 55 bpm no
  sinus interval after the first is excluded and the clean tolerances hold (the first can be,
  §8). At 40 and 45 bpm, §8's limit: no decision contradicts the rule on the true intervals,
  except one within ± 0.5 % of the limit (the fiducial's timing error decides those; amended
  2026-10-31, #40), and `hrv` is computed from the rest or null with its reasons; no tolerance
  against the unfiltered truth applies.
- **Motion bursts** are not exempt (amended 2026-10-31, #40): every accepted strip meets the noisy
  tolerances, or its `hrv` is null. **HR near the 220 bpm limit**: the HR assertion leaves out a
  strip with a true interval within ± 5 ms of 272.7 ms; #40 counts those strips and reports them.
- **AF-like strips** have no HRV ground truth. On their fixed seeds: the gate as in the table, and
  `hrv` null with `too_many_excluded` (never a display label, §9). For every strip of any
  condition: more than 20 % excluded means `hrv` null with `too_many_excluded`.

RMSSD tolerance, by the generator's mean HR:

| HR | Clean and respiratory | Noisy and ectopic |
|---|---|---|
| 40 to under 60 bpm | ± 6.0 ms | ± 10.5 ms |
| 60 to under 100 bpm | ± 4.0 ms | ± 7.0 ms (amended 2026-10-31, #88) |
| 100 to under 140 bpm | ± 5.0 ms | ± 8.0 ms |
| 140 to 180 bpm | ± 5.5 ms | ± 8.5 ms |

The RMSSD and SDNN tolerances assume R-peak timing error no worse than independent Gaussian error
of SD 1.41 ms in the clean conditions (RR error RMS 2 ms) and 2.12 ms in the noisy and ectopic
ones. Each absolute value is the largest error in the simulation note for its conditions plus at
least 20 %, rounded up to 0.5 ms; each held in every simulated strip. Timing error of SD σ inflates
RMSSD by about √(RMSSD² + 6σ²) − RMSSD, so from 100 bpm, where the median true RMSSD of the
default variability is 6.0 ms (100–139 bpm) and 3.4 ms (140–180 bpm), the tolerance is of the
order of the value itself. There version 1 claims only this absolute bound, no relative accuracy.

The margins hold for the generator above. #40 reports its generator's median true RMSSD per band
under the default variability in `docs/dev/ecg.md`, next to the note's (31.0, 13.2, 6.0 and
3.4 ms), as evidence that the tolerances apply to its strips; a median more than 10 % off goes to
the Architect before the tolerances are used. A seeded case that misses a tolerance is in its
tail: #40 reports it (§13) and never re-seeds it.

## SQI bounds and components (§7)

At 300 and 500 Hz, on clean strips unless stated:

- A = 12 mV at 60 bpm fails with `amplitude_out_of_range`; A = 0.05 mV fails with `flat_line`.
- Constant RR at 25 bpm and at 230 bpm fails, with `implausible_rate` among the reasons.
- kSQI (Pearson kurtosis of the conditioned window): a clean strip at 60 bpm > 5 (Li 2008); white
  Gaussian noise, in the window from 10 to 20 s of a 30-s strip, within 3 ± 1.
- pSQI on 30-s sinusoids, every window: 10 Hz ≥ 0.99, 30 Hz ≤ 0.01. basSQI likewise: 0.3 Hz
  ≤ 0.01, 10 Hz ≥ 0.99.
- The first and last window leave the strip's first and last 0.5 s out of kSQI and pSQI (§7;
  amended 2026-10-31, #40): white Gaussian noise gives kSQI within 3 ± 1 in the first and the
  last window too.
- A DC offset: the same 1 mV sinusoids plus 1 mV of DC give the same basSQI bounds (0.3 Hz
  ≤ 0.01, 10 Hz ≥ 0.99), as §7 subtracts each window's mean before Welch.

## Seeds and other tests

Seeds: ≥ 20 per condition and rate in CI; a ≥ 200-seed `slow` sweep run by #40 and by every PR
that bumps a module version, its numbers in `docs/dev/ecg.md`. Also: the filter responses above; a
property test that a failed gate yields no downstream value; determinism and a golden output (a
golden change without a version bump fails); a guard against recording formats (WFDB, EDF,
SCP-ECG, HL7 aECG, DICOM waveform) and files over 256 KB under `backend/tests/ecg/`. A row read
per heart-rate band has its own ≥ 20 seeds in CI (amended 2026-10-31, #40).

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
  respiratory strips miss "within one pair". On the default series with band-limited
  interpolation, 2,000 strips per bpm from 40 to 180 (bases 811 and 812): no miss of one pair
  plus the band at 1.41 ms, nor of two pairs plus the band at 2.12 ms, in 282,000 strips each;
- RMSSD and SDNN with Gaussian R-peak timing error of SD 1.41 ms (clean) and 2.12 ms (noisy), on
  the generator's RR series (the default one with band-limited interpolation): 2,000 strips per
  whole bpm from 40 to 180 per case (respiratory: 55 to 180), plus 20,000 per bpm at 40 to 45 and
  at the band edges 59, 60, 99, 100, 139, 140 and 180 bpm (respiratory: 55, 59 and the other
  edges); numpy 2.5.4, `default_rng([base, bpm])` with the bases listed. Largest |RMSSD error| in
  ms over both samples:

  | Case (seed bases) | 40–59 bpm | 60–99 bpm | 100–139 bpm | 140–180 bpm |
  |---|---|---|---|---|
  | Default variability, 1.41 ms (611, 711) | 4.84 | 2.91 | 3.82 | 3.85 |
  | Respiratory, 1.41 ms (621, 721) | 2.76 | 2.22 | 1.00 | 0.91 |
  | Ectopic, 1.41 ms (613, 713) | 4.82 | 2.62 | 2.62 | 3.27 |
  | Default variability, 2.12 ms (612, 712) | 6.90 | 5.32 | 5.90 | 6.95 |
  | Ectopic, 2.12 ms (614, 714) | 8.51 | 4.26 | 4.82 | 5.29 |

  The noisy 40–59 bpm value rests on one ectopic strip at 41 bpm; 3 of 118,805 ectopic strips at
  40–45 bpm exceed 7.0 ms. Clean 60–99 bpm is set at 4.0 ms although 3.5 ms is 20.3 % above 2.91 ms,
  since the review's own sample reached 2.96 ms. Noisy 60–99 bpm is set at 7.0 ms, not 6.5 ms
  (amended 2026-10-31, #88), on the same rule: the #46 review's own samples reached 5.45 ms (one
  default-variability strip at 96 bpm in 1,000 strips per bpm, a 19.2 % margin against 6.5 ms; 5.45
  × 1.2 = 6.54, rounded up to 0.5 ms) and at most 5.35 ms in 760,000 strips (20,000 per bpm at 61–98
  bpm). The largest SDNN error is 2.46 ms clean (default, 40 bpm) and 3.66 ms noisy (ectopic, 45
  bpm). The mean RMSSD error under the default variability is +0.20, +0.48, +0.96 and +1.47 ms at
  1.41 ms, and +0.45, +1.05, +1.97 and +2.80 ms at 2.12 ms; the median true RMSSD is 31.0, 13.2, 6.0
  and 3.4 ms (25.7, 11.1, 5.1 and 2.9 ms with linear interpolation at 1 Hz). The earlier ± max(2 ms,
  5 %) misses in 3 % of clean strips at 40 bpm, 2 % at 120, 5 % at 140, 13 % at 160 and 25 % at 180
  bpm. No exclusion differed from the truth (respiratory: none after the first interval);
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
- E6 with several trigger recordings, counting only those with a gate result: hidden with 1 of 2
  and 2 of 3 failed; shown with 1 of 3 failed and 2 passed; "not evaluated" when none has a
  result. An unlinked trigger group and a linked recording without a result give the same
  outcome: fail, pass and an unlinked group, and fail, pass and a recording without a result,
  are both hidden (1 of 2 counted). A failure never counts as a pass.
- Only the trigger counts, in both directions. A failing trigger strip with a 10-strip baseline
  that all passed hides E6, as it hides E4 on the same strip; a passing trigger strip with 8 of
  its 10 baseline strips failed shows E6; a trigger without a gate result shows "not evaluated"
  whatever the baseline's results. In a multi-day window, a trigger group that also lies in a
  later day's baseline counts as a trigger.
- The trigger is the value's provenance, read from the evidence, here on daily values from #37's
  `daily_values` with the source groups the M4 wiring issue adds. A morning value: a failing
  morning strip with two passing retakes the same day hides E6, as only the morning group counts;
  a passing morning strip with one failing retake shows it. A median value (no morning reading):
  2 of 3 of the day's strips failed hides E6; 1 of 3 failed shows it. The counted set is exactly
  the provenance: a value whose provenance names one group of a three-strip day counts only that
  group, whatever the other two strips' results.
- The link is resolved at read time: a recording linked, deleted or recomputed after the finding
  changes the next read, with no write to the finding.
- The read function, for every output (viewer, API, export, doctor PDF, AI context): HR and HRV
  from a stale `estimated` (an `hrv`-only version difference, a different `input_fingerprint`)
  are left out; R-peak markers, SQI values and gate reasons are left out when §11's rule makes it
  absent and kept on an `hrv`-only difference. The guard test fails for a module outside the read
  function that reads findings or `estimated` for presentation, and passes the rules engine's
  writes, the `estimated` recompute and its sweep, the account-delete and purge path, and
  `ownvitals ecg validate` (#47).
- The hold, with a fake clock and the bound changed through its setting. An E4 or E6 notification
  is not sent while a trigger group can still be linked or a linked recording lacks a current
  `estimated`, and the first revision is younger than the bound. A pass sends it; a failure never
  does, even after a later recompute passes; a no-result `estimated` sends it marked "not
  evaluated". Just before the bound it is still held; at the bound it is sent marked "not
  evaluated". A link that appears during the hold counts. A new revision restarts the hold for
  that revision, the bound counted from the first; a withdrawn finding sends nothing. E1 to E3 are
  sent at once. A later failure recalls nothing.
- The hold waits only for trigger recordings: a baseline recording without a current `estimated`
  never holds it. With no linkable recording it ends at once and sends the notification marked "not
  evaluated", well before the bound: for each `ecg` capability state `unsupported`, `not_authorized`
  and `not_available`, and once an `ecg` sync that started after the trigger was stored completes
  successfully without a match. A sync that started before, failed, or completed only partly doesn't
  end the hold. A linked recording kept with `signal` NULL has, once the recompute that #25 enqueues
  at storage has run (§1), an `estimated` with `signal_missing`, which gives no gate result. When
  that recording is the trigger's only linked one and no other trigger group can still be linked,
  the hold then ends at once and the notification is sent marked "not evaluated"; otherwise §11
  governs (amended 2026-10-31, #88). The hold records how it ended (gate result, no link possible,
  bound) as a reason code, and a link that appears after a "no link possible" end is counted, for A9
  (count only).

## References

The sources cited in ADR 0006 and in this file, moved here verbatim from the end of the ADR to keep
the ADR within 500 lines (amended 2026-10-31, #88).

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
- Malik M, et al. Heart rate variability in relation to prognosis after myocardial infarction:
  selection of optimal processing techniques. Eur Heart J 1989;10(12):1060–1074.
- Martínez JP, Almeida R, Olmos S, Rocha AP, Laguna P. A wavelet-based ECG delineator: evaluation
  on standard databases. IEEE Trans Biomed Eng 2004;51(4):570–581.
- McSharry PE, Clifford GD, Tarassenko L, Smith LA. A dynamical model for generating synthetic
  electrocardiogram signals. IEEE Trans Biomed Eng 2003;50(3):289–294.
- Moody GB, Muldrow WE, Mark RG. A noise stress test for arrhythmia detectors. Computers in
  Cardiology 1984;11:381–384.
- Munoz ML, van Roon A, Riese H, et al. Validity of (ultra-)short recordings for heart rate
  variability measurements. PLoS ONE 2015;10(9):e0138921.
- Orphanidou C, et al. Signal-quality indices for the electrocardiogram and photoplethysmogram:
  derivation and applications to wireless monitoring. IEEE J Biomed Health Inform 2015;19:832–838.
- Pan J, Tompkins WJ. A real-time QRS detection algorithm. IEEE Trans Biomed Eng 1985;32:230–236.
- Peltola MA. Role of editing of R–R intervals in the analysis of heart rate variability. Front
  Physiol 2012;3:148.
- Shaffer F, Ginsberg JP. An overview of heart rate variability metrics and norms. Front Public
  Health 2017;5:258.
- Task Force of the ESC and NASPE. Heart rate variability: standards of measurement, physiological
  interpretation and clinical use. Circulation 1996;93(5):1043–1065.

## Amendments

The dated amendments of this file and of ADR 0006 are in
[0006-ecg-subsystem-amendments.md](0006-ecg-subsystem-amendments.md).
