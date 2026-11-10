# ECG signal processing (`ownvitals.ecg`)

The pure library of ADR 0006 ([docs/adr/0006-ecg-subsystem.md](../adr/0006-ecg-subsystem.md),
the "ADR"; its tests are in [0006-ecg-subsystem-tests.md](../adr/0006-ecg-subsystem-tests.md),
the "tests file"; the dated amendments and their evidence are in
[0006-ecg-subsystem-amendments.md](../adr/0006-ecg-subsystem-amendments.md)). Issue #40, status IMPLEMENTED: code and synthetic tests only. Nothing here is
verified against a real Withings recording (INTEGRATION_VERIFIED is the owner's, §14 V2).

No I/O, no database, no network, no clock, no randomness: `analyse(leads, sampling_hz)` is a
function of its arguments. The purity guards (`tests/test_analysis_purity.py`) cover the package.

## Layout

| Module | Version | What it does |
|---|---|---|
| `input.py` | 1.0.0 | Validates leads and rate, scales µV to mV, rejects what cannot be a 30 s strip |
| `filters.py` | 1.0.0 | Zero-phase SOS cascade with odd padding (baseline, mains, high-cut) |
| `rpeaks.py` | 3.0.0 | Pan-Tompkins (primary) and Elgendi (secondary); `Detection.confirmed` |
| `sqi.py` | 4.0.0 | Windowed SQI, clipping, the gate, per-beat distortion, the verified span |
| `hrv.py` | 5.0.0 | NN-interval classification, HR, SDNN, RMSSD, pNN50 |
| `pipeline.py` | `ecg-5.0.0` | `analyse`, `to_estimated`, `canonical_json`, `MODULES`, `GATE_MODULES` |

Tests are in `backend/tests/ecg/`; the generator is `backend/tests/ecgsynth.py` (McSharry dynamical
model, every event amplitude scaled by ω_k / ω̄ so that a beat does not depend on its RR). Noise
models are `ecgnoise.py`; conditions and seeds `scenarios.py`; every threshold in `tolerances.py`
(one table, each row cites the tests file).

## Commands (from `backend/`)

```bash
uvx uv@0.13.6 run pytest tests/ecg                     # CI size: 20 seeds per condition and rate
OWNVITALS_ECG_SLOW=1 uvx uv@0.13.6 run pytest tests/ecg/test_ecg_sweep.py   # 200 seeds, ~10 min
uvx uv@0.13.6 run python -u tests/ecg/calibrate.py --seeds 200 # the tables of this file
```

Re-record the golden file after a deliberate change **and** a version bump (module version and
`PIPELINE_VERSION`); a golden change without a new pipeline version fails
`test_a_golden_change_needs_a_new_pipeline_version`:

```bash
uv run python -c "import sys; sys.path[:0] = ['tests', 'tests/ecg']; \
import test_ecg_golden as g; g.record()"
```

Versions (ADR §15): a change that can alter a stored gate result or value is a major bump for the
module and the pipeline. `rpeaks` 3.0.0 (confirmed beats; the matching takes the nearest free candidate), `sqi` 4.0.0
(confirmed beats, verified span, edge margin; bSQI uses the matching), `hrv` 5.0.0
(`unconfirmed_beat`, `distorted_beat`, `missed_beat`, null HR with `irregular_rhythm`).

## Choices the specification leaves open

- **Pan-Tompkins start.** Signal level starts at 0.75 and noise level at 0.5 of the maximum of the
  first 2 s (`INIT_SIGNAL`, `INIT_NOISE`); the rate counts as irregular from 3 intervals.
- **Confirmed beats.** A Pan-Tompkins beat goes downstream only if an Elgendi detection lies within
  ±50 ms (bSQI's own matching). An unconfirmed beat is dropped; the interval bridging it is
  excluded with reason `unconfirmed_beat` and counts toward the excluded fraction.
- **Edge margin.** The first and last 0.5 s of the strip (`SETTLE_MS`) are left out of the kSQI
  and pSQI of the first and last window: odd padding turns an edge sample into a step. They do
  not gate.
- **Distorted beats.** A confirmed beat whose Pearson r against the median beat of its window,
  over R ± 100 ms, is under `DISTORTED_R` = 0.85 is distorted. Intervals touching it leave the
  NN intervals (`distorted_beat`) and count in the excluded fraction; HR keeps them. tSQI is
  unchanged (it uses half the median RR as its segment).
- **Verified span.** A distorted beat within `EDGE_TRIM_S` = 5 s of an end removes every beat
  between it and that end; the span is measured on the beats left and under 20 s gives
  `QualityRejected` with `too_short`. An unassessed beat (NaN r) is not distorted.
- **Missed beats** (`hrv.missed_beats`, 4.0.0). On a strip whose other exclusions are at most
  20 % of the intervals, an interval of at least 1.75 times the median of its up to eight
  neighbours (four each side, bridging intervals left out, at least three neighbours) leaves HR
  and NN as `missed_beat` and counts in the excluded fraction. Over 200 seeds: clean 1.10 at
  most, respiratory 1.23, ectopic 1.45 (the pause), AF-like up to 3.2 (rule off); 3 intervals
  flagged in all, all real misses (6 dB white noise at 173-174 bpm), no true interval.
- **Irregular rhythm** (5.0.0). When the intervals excluded by (0), (a), (c) and (b) are more
  than 20 % of all (the guard that switches `missed_beat` off), `hr_bpm` is null and
  `hrv_reasons` ends with `irregular_rhythm`: with no neighbouring rhythm to compare with a
  missed beat would corrupt the mean, and no unchecked value is shown (Withings' own HR is the
  value to show). Every AF-like strip has null HR; 1 of 200 ectopic strips at 300 Hz has too.
  HR is compared with the truth on every accepted strip that has one, whether or not `hrv` is
  computed (columns "HR strips", "HR null" below).
- **Classification order** in `hrv.classify`: unconfirmed, out_of_range, missed, distorted,
  deviation. The deviation reference is the first five intervals that are none of those.
- **HR assertion.** A strip with a true interval within ±5 ms of 60000 / 220 ms is left out of the
  HR comparison (`HR_BORDERLINE_MS`; `measure.hr_borderline`), like the ±0.5 % rule for the
  respiratory 20 % exclusion.
- **Edge padding.** `test_ecg_filters.py` has a test of the kurtosis of white noise at the edges
  (the padding issue behind the edge margin).

## Numbers (synthetic, 200 seeds)

Produced by `python -u tests/ecg/calibrate.py --seeds 200` on 2026-11-01 at the head of the PR (hrv 5.0.0);
the seeds are those of `scenarios.py` (strip `i` of 200 is `make_strip(i, hr_i, fs)`, heart rates
spread evenly over the band; its noise generator is `default_rng([i, crc32(condition), fs])`);
the RMSSD medians use seeds 5000 to 5009, 10 strips per whole bpm at 300 Hz. The slow sweep
(`test_ecg_sweep.py`) asserts the same rows from the same `calibrate.survey`, so the document and
the tests cannot disagree. Everything below is a property of the generator, not of a heart. "Worst HR" is the worst HR
error over all "HR strips" (every accepted strip with a non-null HR, with or without HRV), as a
share of its tolerance; AF-like strips have no HR to compare.

#### Median true RMSSD of the generator (10 strips per whole bpm, seeds 5000..5009, 300 Hz)

| Band (bpm) | Generator median (ms) | Strips | Note (ms) | Difference |
|---|---|---|---|---|
| 40-59 | 31.61 | 200 | 31.0 | +2.0 % |
| 60-99 | 13.29 | 400 | 13.2 | +0.7 % |
| 100-139 | 5.76 | 400 | 6.0 | -4.0 % |
| 140-180 | 3.38 | 410 | 3.4 | -0.6 % |

#### Conditions (200 seeds per condition and rate)

| Condition | Hz | Gate pass % | Pooled Se % | Pooled +P % | Min strip Se % | Min strip +P % | HR strips | HR null | HRV strips | Worst HR | Worst SDNN | Worst RMSSD | Worst pNN50 | Max RR rms (ms) | HR borderline |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| clean | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.09 | 0.08 | 0.07 | 0.2 | 0 |
| clean | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.11 | 0.10 | 0.14 | 0.3 | 0 |
| clean_lt50 | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.13 | 0.10 | 0.33 | 0.3 | 0 |
| clean_lt50 | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.10 | 0.08 | 0.20 | 0.3 | 0 |
| white12_ge50 | 300 | 97.00 | 100.00 | 100.00 | 100.00 | 100.00 | 194 | 0 | 194 | 0.06 | 0.23 | 0.20 | 0.25 | 3.3 | 0 |
| white12_ge50 | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.06 | 0.18 | 0.18 | 0.33 | 2.5 | 0 |
| white12_lt50 | 300 | 6.50 | 100.00 | 100.00 | 100.00 | 100.00 | 13 | 0 | 13 | 0.05 | 0.41 | 0.28 | 0.33 | 3.6 | 0 |
| white12_lt50 | 500 | 49.00 | 100.00 | 100.00 | 100.00 | 100.00 | 98 | 0 | 98 | 0.05 | 0.28 | 0.25 | 0.43 | 3.6 | 0 |
| white6 | 300 | 78.50 | 99.95 | 100.00 | 98.84 | 100.00 | 157 | 0 | 157 | 0.15 | 0.28 | 0.41 | 0.18 | 4.8 | 0 |
| white6 | 500 | 84.50 | 99.99 | 100.00 | 98.84 | 100.00 | 169 | 0 | 169 | 0.07 | 0.26 | 0.43 | 0.22 | 5.5 | 0 |
| emg6 | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.07 | 0.60 | 0.50 | 0.50 | 6.9 | 0 |
| emg6 | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.06 | 0.37 | 0.35 | 0.33 | 5.6 | 0 |
| wander_ge50 | 300 | 99.50 | 100.00 | 100.00 | 100.00 | 100.00 | 199 | 0 | 199 | 0.05 | 0.05 | 0.03 | 0.08 | 0.2 | 0 |
| wander_ge50 | 500 | 98.50 | 100.00 | 100.00 | 100.00 | 100.00 | 197 | 0 | 197 | 0.05 | 0.05 | 0.03 | 0.00 | 0.2 | 0 |
| wander_lt50 | 300 | 84.00 | 100.00 | 100.00 | 100.00 | 100.00 | 168 | 0 | 168 | 0.05 | 0.08 | 0.06 | 0.20 | 0.4 | 0 |
| wander_lt50 | 500 | 84.00 | 100.00 | 100.00 | 100.00 | 100.00 | 168 | 0 | 168 | 0.05 | 0.06 | 0.04 | 0.14 | 0.3 | 0 |
| mains | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.05 | 0.06 | 0.04 | 0.00 | 0.4 | 0 |
| mains | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.05 | 0.06 | 0.05 | 0.12 | 0.4 | 0 |
| amplitude_0.3mv | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.09 | 0.07 | 0.07 | 0.2 | 0 |
| amplitude_0.3mv | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.11 | 0.08 | 0.14 | 0.3 | 0 |
| inverted | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.09 | 0.08 | 0.07 | 0.2 | 0 |
| inverted | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.11 | 0.10 | 0.14 | 0.3 | 0 |
| af | 300 | 100.00 | 99.59 | 100.00 | 95.08 | 100.00 | 0 | 200 | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.0 | 0 |
| af | 500 | 100.00 | 99.47 | 100.00 | 94.67 | 100.00 | 0 | 200 | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.0 | 0 |
| ectopic | 300 | 99.50 | 100.00 | 100.00 | 100.00 | 100.00 | 198 | 1 | 198 | 0.08 | 0.06 | 0.03 | 0.14 | 1.1 | 6 |
| ectopic | 500 | 99.50 | 100.00 | 100.00 | 100.00 | 100.00 | 199 | 0 | 198 | 0.08 | 0.06 | 0.05 | 0.14 | 1.1 | 5 |
| motion_edge | 300 | 99.50 | 99.98 | 99.99 | 98.18 | 95.83 | 199 | 0 | 199 | 0.80 | 0.10 | 0.06 | 0.08 | 1.1 | 0 |
| motion_edge | 500 | 99.50 | 99.94 | 99.99 | 98.33 | 96.43 | 199 | 0 | 199 | 1.11 | 0.06 | 0.17 | 0.12 | 1.1 | 0 |
| respiratory | 300 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.25 | 0.20 | 0.17 | 0.7 | 0 |
| respiratory | 500 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 | 200 | 0 | 200 | 0.10 | 0.21 | 0.20 | 0.43 | 0.6 | 0 |

#### Worst RMSSD error by band, ms (tolerance in brackets)

| Condition | Hz | 40-59 | 60-99 | 100-139 | 140-180 |
|---|---|---|---|---|---|
| clean | 300 | 0.48 (6.0) | 0.25 (4.0) | 0.10 (5.0) | 0.07 (5.5) |
| clean | 500 | 0.58 (6.0) | 0.24 (4.0) | 0.10 (5.0) | 0.06 (5.5) |
| clean_lt50 | 300 | 0.60 (6.0) | - | - | - |
| clean_lt50 | 500 | 0.45 (6.0) | - | - | - |
| white12_ge50 | 300 | 2.12 (10.5) | 1.29 (7.0) | 0.90 (8.0) | 0.85 (8.5) |
| white12_ge50 | 500 | 1.91 (10.5) | 1.05 (7.0) | 0.45 (8.0) | 0.62 (8.5) |
| white12_lt50 | 300 | 2.99 (10.5) | - | - | - |
| white12_lt50 | 500 | 2.67 (10.5) | - | - | - |
| white6 | 300 | - | 2.90 (7.0) | 2.70 (8.0) | 2.59 (8.5) |
| white6 | 500 | 0.62 (10.5) | 3.02 (7.0) | 1.99 (8.0) | 2.31 (8.5) |
| emg6 | 300 | 5.21 (10.5) | 3.03 (7.0) | 2.24 (8.0) | 2.16 (8.5) |
| emg6 | 500 | 3.26 (10.5) | 2.42 (7.0) | 1.35 (8.0) | 1.77 (8.5) |
| wander_ge50 | 300 | 0.30 (10.5) | 0.23 (7.0) | 0.09 (8.0) | 0.06 (8.5) |
| wander_ge50 | 500 | 0.30 (10.5) | 0.19 (7.0) | 0.08 (8.0) | 0.06 (8.5) |
| wander_lt50 | 300 | 0.60 (10.5) | - | - | - |
| wander_lt50 | 500 | 0.44 (10.5) | - | - | - |
| mains | 300 | 0.38 (10.5) | 0.25 (7.0) | 0.10 (8.0) | 0.06 (8.5) |
| mains | 500 | 0.48 (10.5) | 0.31 (7.0) | 0.10 (8.0) | 0.07 (8.5) |
| amplitude_0.3mv | 300 | 0.38 (6.0) | 0.27 (4.0) | 0.10 (5.0) | 0.07 (5.5) |
| amplitude_0.3mv | 500 | 0.48 (6.0) | 0.24 (4.0) | 0.10 (5.0) | 0.06 (5.5) |
| inverted | 300 | 0.48 (6.0) | 0.25 (4.0) | 0.10 (5.0) | 0.07 (5.5) |
| inverted | 500 | 0.58 (6.0) | 0.24 (4.0) | 0.10 (5.0) | 0.06 (5.5) |
| ectopic | 300 | 0.34 (10.5) | 0.20 (7.0) | 0.12 (8.0) | 0.07 (8.5) |
| ectopic | 500 | 0.37 (10.5) | 0.37 (7.0) | 0.12 (8.0) | 0.09 (8.5) |
| motion_edge | 300 | 0.59 (10.5) | 0.27 (7.0) | 0.09 (8.0) | 0.07 (8.5) |
| motion_edge | 500 | 1.83 (10.5) | 0.22 (7.0) | 0.71 (8.0) | 0.10 (8.5) |
| respiratory | 300 | 1.03 (6.0) | 0.82 (4.0) | 0.36 (5.0) | 0.14 (5.5) |
| respiratory | 500 | 0.97 (6.0) | 0.78 (4.0) | 0.46 (5.0) | 0.13 (5.5) |

#### Gate pass by heart-rate band (below 50 bpm: 40 seeds; from 50 bpm: 200 seeds)

| Condition | Hz | Below 50 bpm | From 50 bpm |
|---|---|---|---|
| clean | 300 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| clean | 500 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| white12 | 300 | 4/40 (10.0 %) | 194/200 (97.0 %) |
| white12 | 500 | 22/40 (55.0 %) | 200/200 (100.0 %) |
| white6 | 300 | 0/40 (0.0 %) | 168/200 (84.0 %) |
| white6 | 500 | 0/40 (0.0 %) | 180/200 (90.0 %) |
| emg6 | 300 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| emg6 | 500 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| wander | 300 | 34/40 (85.0 %) | 199/200 (99.5 %) |
| wander | 500 | 38/40 (95.0 %) | 197/200 (98.5 %) |
| mains | 300 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| mains | 500 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| amplitude_0.3mv | 300 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| amplitude_0.3mv | 500 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| inverted | 300 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| inverted | 500 | 40/40 (100.0 %) | 200/200 (100.0 %) |
| ectopic | 300 | 38/40 (95.0 %) | 200/200 (100.0 %) |
| ectopic | 500 | 39/40 (97.5 %) | 199/200 (99.5 %) |
| motion_edge | 300 | 38/40 (95.0 %) | 199/200 (99.5 %) |
| motion_edge | 500 | 38/40 (95.0 %) | 199/200 (99.5 %) |

#### Conditions the gate must reject (share that passes)

| Condition | Hz | Passes |
|---|---|---|
| motion_half | 300 | 1/200 |
| motion_half | 500 | 1/200 |
| dropout_start | 300 | 0/200 |
| dropout_start | 500 | 0/200 |
| dropout_middle | 300 | 0/200 |
| dropout_middle | 500 | 0/200 |
| dropout_end | 300 | 0/200 |
| dropout_end | 500 | 0/200 |
| flat_line | 300 | 0/200 |
| flat_line | 500 | 0/200 |
| all_zeros | 300 | 0/200 |
| all_zeros | 500 | 0/200 |
| clipping | 300 | 0/200 |
| clipping | 500 | 0/200 |

## Tails of the 200-seed sweep (accepted by amendment, not re-seeded)

The sweep has 70 rows and all pass, with the two tails named below (run 2026-11-01, 7 min). Tail cases, with the numbers:

1. **AF-like, 500 Hz: lowest per-strip Se 94.67 %.** The floor of this row is 94.5 %
   (`tolerances.STRIP_SE_FLOOR_OVERRIDES`; amendments file, 2026-11-01 (#40) items 10 and 11); at 300 Hz
   the lowest is 95.08 %. Pooled Se 99.47 %, +P 100 % (tp 11,909, fn 63, fp 0). A detection metric
   only: AF-like strips have no HR (null, `irregular_rhythm`). The earlier premise that their HR
   is within tolerance was wrong (up to 1.98 bpm with a missed beat) and is withdrawn.
2. **Burst strip 18 of 200 at 500 Hz** (52.66 bpm): HR 53.8, 1.112 times the tolerance, named in
   `KNOWN_TAIL`. At 300 Hz the worst is 0.80.
3. **6 dB white noise at 173-174 bpm** (strips 190, 191 at 300 Hz, 189 at 500 Hz) had HR 2.05
   times the tolerance (one missed beat); fixed by the `missed_beat` rule: HR margin now 0.15
   and 0.07.
4. **The HR-borderline strips** (a true interval within 5 ms of 272.7 ms, left out of the HR
   assertion): ectopic only, 6 of 200 strips at 300 Hz and 5 of 200 at 500 Hz, 11 of 400 (column "HR borderline" above). The 135 bpm,
   500 Hz ectopic strip has premature intervals of 269.5 and 272.1 ms; the wide ectopic QRS puts
   the detected intervals over the limit.

## Known limitations

- **White noise at 12 dB, and baseline wander, below 50 bpm** may be rejected by the gate (table
  above: white noise passes 4 and 22 of 40, wander 34 and 38 of 40). Elgendi's threshold scales
  with the mean squared signal, small when QRS are sparse, so wide noise blocks pass and bSQI
  falls under 0.90; for wander, the clipping rule reads the smooth extremum of a 1 mV sinusoid
  (within the 1 µV rounding for about 47 ms) as a plateau at the strip's raw maximum. Rejected
  strips show no values. Candidate for a later SQI version, if §14 V2 shows clipping-reason
  failures at low HR on real strips: a clipping run counts only if both ends are entered and
  left by a step.
- Motion-burst strips: lowest per-strip Se 98.18 % and +P 95.83 % (300 Hz), 98.33 % and 96.43 %
  (500 Hz); gate pass 99.5 % at both rates.

## Unverified, assumed

- Every number is from generated strips. No real or public recording was used.
- The distortion threshold 0.85, the 5 s edge trim, the ±50 ms confirmation and the 0.5 s edge
  margin are derived from the generator (derivation and per-condition r table: amendments file,
  2026-10-31 (#40), item 6). Whether real strips behave like them is §14 V2.
- The generator's AF-like and ectopic beats are simplifications of real ones.
- CI cost: the ECG suite takes about 75 s on the Windows development machine (480 tests), the
  cost of at least 20 seeds in 25 conditions at two rates (accepted by the Architect).

## Validation (`ecg validate`, #47)

Owner-run command of ADR 0006 §14. Agents do not run it against a real account and do not treat
its output as settling A1–A9 or as INTEGRATION_VERIFIED. The criteria live in `ownvitals.ecg.validate_cli` and `ownvitals.ecg.validate_rules`
(pure: no database; split so each file stays short). `ownvitals.ecg_validate` opens one
`user_session` for `--user` and is the only new module on the session allowlist; the signal
library stays pure. `cli.py` only installs the subcommand.

```bash
python -m ownvitals ecg validate --user <uuid> [--oc1 yes|no] [--oc2 yes|no] \
  [--v4-wrong-beats N --v4-inspected-beats N --v4-justified N]
```

Stdout is categorical: pass/fail, yes/no, counts and rates rounded half up to a whole percent.
No samples, measurement values, timestamps or ids. A database error on stderr is the exception
type only. Exit 0 when no decided check failed (pending and not-applicable do not fail). Exit 1
when one did, or when the user id or the V4 flags are refused. V2 is report-only.

| Line | Meaning |
|---|---|
| `recordings N` | rows visible for that user |
| `OC1 scale` | pass when every first lead's peak absolute sample is 100–10,000 µV |
| `OC1 owner` | the `--oc1` answer, or `pending` |
| `OC1` | pass only when the scale passes and the owner answers yes |
| `OC2 positions N` | how many distinct wear-position codes are stored (not the codes) |
| `OC2` | pass only when the owner answers yes and at least one code is stored |
| `OC3 lowpass` | pass when, on every strip long enough for a 4 s Welch spectrum, power from 5–15 Hz is ≥ 30 dB above power above 40 Hz (the ADR's figure), on the stored signal |
| `OC3 mains` | yes when a 50 or 60 Hz line's ±1 Hz power is ≥ 10 dB above the side bands 3–10 Hz away and ≥ 1% of the 5–15 Hz power (our choices for the ADR's yes/no) |
| `OC4` | not-applicable when every recording has one lead. Otherwise pass when each extra lead has the same length and ≥ 95% of R-peaks (both directions) lie within 20 ms. One sampling rate is stored per recording, so "equal rate" is that single rate; an unsupported rate fails toward "not simultaneous" |
| `OC5` | pass when every recording shares connection and timestamp with a live measure group that has a type in {130, 135, 136, 137, 138} and the same device id (or, when the recording names a device and the group has none, the same model). A recording with no device id does not match. `OC5 without` is the count with no such group |
| `OC5 late-link` | `not-applicable` until the notification hold records why it ended (#88) |
| `V1` | pass when the gate passes ≥ 80% of recordings whose `afib_details` is not in {−1, 2, 5, 13}. A missing code counts as not noisy |
| `V2` | six agreement counts (Withings clean/noise × our pass/fail/none). HRV-reason lines are the share of gate-passed strips carrying that reason. Fail-band lines count gate failures by Withings heart-rate band (under 50, from 50, unknown) and reason. Unknown reason strings are counted as `other` and not printed |
| `V3` | pass when \|our HR − Withings heart rate\| ≤ 5 bpm for ≥ 95% of gate-passed strips that have both rates and are not `irregular_rhythm`. The Bland–Altman summary is the sign of the mean difference and whether both limits of agreement (mean ± 1.96 SD, n−1) lie inside ±5 bpm. The bpm figures are not printed |
| `V4` | pending until the owner passes all three counts. Pass when ≥ 20 gate-passed and ≥ 10 gate-failed strips are stored, the wrong-beat percent is ≤ 1, and ≥ 8 rejected strips were justified. The command does not list which strips: a list would identify recordings |
| `V5` | pass when recomputing `estimated` matches the stored JSON byte for byte (`canonical_json`), including the stored input revision and fingerprint. A row with neither a signal nor an estimate is not compared |

The command was not run on a real account. Tests use labelled synthetic values only.

## ECG list, signal and PDF (#192, ADR 0006 section 3)

| Route | Answers |
|---|---|
| `GET /api/ecg?limit=&offset=` | The user's recordings, newest first: local time with zone, device, Withings' classification (attributed), reported heart rate and the linked 135-138 values ("not reported" is an absent value, never 0) |
| `GET /api/ecg/{id}` | The same plus the stored signal (int32 µV per lead, unfiltered), the sampling rate and the lead label ("lead unverified", A2). `ownvitals_estimate` is `null` without the owner-only flag; with it, it holds our estimate from `read_ecg_estimates`, labelled "Estimated by ownvitals from the raw signal; not a Withings measurement" |
| `GET /api/ecg/{id}.pdf` | The true-scale A4 landscape PDF, as an attachment. 404 when there is no signal, 422 over 120 s |

Another user's recording answers 404 (forced RLS plus an explicit `owner_id` filter). The signal
is decoded in `views/ecg_read.py` and never logged; a frame that does not decode is logged by
exception class only and reads as "no signal".

**PDF.** 10 s rows of 250 mm, 25 mm/s and 10 mm/mV, a 1 mm grid with 5 mm majors, a 1 mV 200 ms
calibration pulse per row, three rows to a page, the median level of each lead as its baseline.
It carries local time with zone, device, rate, the lead label, Withings' values with attribution
and the "not a medical device" line. It has **no estimate and nothing experimental**: the
document's input (`ReportFacts`) has no field for one and the route never calls
`read_ecg_estimates`. A strip longer than 120 s (`MAX_SECONDS`, our limit, not Withings') is
refused.

**Renderer: reportlab 5.1.0** (BSD-3-Clause; brings pillow and charset-normalizer). Server side,
no network, built-in Helvetica, no fonts or images fetched, `invariant=1` so the bytes are
deterministic. Alternatives not taken: WeasyPrint (system libraries, CSS layout is not
millimetre-exact), fpdf2 (fewer guarantees on stream determinism). The layout
(`reports/ecg_layout.py`) is pure and emits drawing operations in millimetres; the renderer
(`reports/ecg_pdf.py`) only draws them. Tests measure both the operations and the PDF's own path
operators (`tests/pdftools.py`).

**Hardening (#301).** The route builds the PDF with `asyncio.to_thread` (about 1.2 s of CPU; same
bytes, `build_pdf` is pure), proven by `tests/test_ecg_pdf_route_offloop.py`. The renderer clips
each trace to its own row's grid (plus and minus 2 mV, a PDF clip path inside `q`/`Q`), so a large
excursion never runs over a neighbouring row or the header; the layout still places every sample
unscaled and the clip only cuts the line at the edge (`test_a_trace_beyond_2_mv_is_clipped...`).
`tests/test_ecg_pdf_guard.py` is an AST guard: the ECG and doctor PDF modules make no image or
link calls (`drawImage`, `linkURL`, platypus, URL literals), with planted mutations that must fail.

## Rules E1 to E4 and E6, and the `estimated` recompute (#191)

ADR 0006 §11 and §15, ADR 0010 §7 "ECG". Status IMPLEMENTED: synthetic tests only; nothing here is
verified against a real Withings account.

**Rules** (`analysis/rules/ecg.py` for E1 to E3, `ecg_intervals.py` for E4 and E6; pure). They are
rules of the evaluation pass of #187 (`SPECS`, `derived/evaluate.py`), so they share its replay,
episodes, origin, diff, lock and stale check ([derived.md](derived.md), "Rule evaluation").
Findings hold Withings-reported values only; our gate is the read-time overlay
(`ecg/overlay.py`, #190).

| Rule | Subject | Evaluation days | Trigger groups |
|---|---|---|---|
| E1 `afib` 1 or `afib_details` 1/11/12 (urgent) | `ecg` | days with a recording whose outcome is decided | the groups A7 links to the positive recordings |
| E2 ≥ 3 of the last 10 (by `recorded_at`, up to the day) with `afib_details` 2/5 | `ecg` | same | the groups linked to the counted recordings |
| E3 `afib_details` 6/7 | `ecg` | same | the groups linked to the firing recordings |
| E4 QTc > 450 (male) / 460 (female) / 500 ms (anyone; sex unknown: only 500); PR > 200 ms; QRS ≥ 120 ms | `qtc_interval`, `pr_interval`, `qrs_duration` | days with a reading of the subject | every reading of the day above the band |
| E6 daily value > the finding threshold at the day (k 2.5, 90 days before, BASELINE) | `qtc_interval`, `qrs_duration` | days with a daily value | the daily value's provenance (`metric_daily.source_groups`) |

Choices the specification leaves open:

- **A missing code.** NULL or a code outside withings-findings §2's table is missing. A day on
  which a missing code could change E1's, E2's or E3's outcome is **not an evaluation day**
  (`ecg.decided_days`): it neither opens nor ends an episode, so a missing value never counts as
  negative. E2 with 2 inconclusive and one missing code among the last 10 is undecided; with 2
  and none missing it does not fire.
- **Episodes.** All five open on their first firing and end on the first evaluation that does not
  fire (`advance_simple`); reference day and window end are the evaluation day (E2's window starts
  at the oldest of the counted ten). A later negative recording ends an E1 episode; the ended
  finding stays in the history.
- **E1 to E3's trigger groups** are the live measure groups A7 links to the firing recordings when
  the pass reads them (same connection and timestamp, an ECG type, the same device: the overlay's
  `a7_links`). The read function links them again at read time, so a recording deleted later is
  no link. They show our gate outcome only behind the owner-only flag and are never hidden.
- **E4** is experimental: computed only when the owner-only switch is effective, like B1, B3 and
  C1x. The sex is `user_profiles.sex`.
- **Inputs and provenance.** `derived/findings_ecg.py` reads, in the pass's transaction, the
  recordings' Withings fields (never `signal` or `estimated`), their A7 groups and the profile's
  sex; the evaluation fingerprint covers them, so a new or changed recording between the read and
  the write makes the pass `stale`. `input_revision` stays the `measures` counter. An `ecg` pass
  (sync, backfill, webhook fetch) queues an evaluation; without a `measures` revision row nothing
  is evaluated (the pass's known gap), so an ECG-only account gets no E1 to E6 until a `measures`
  row exists.
- **Codes.** `afib_details` -1 (not processed) and 13 are documented codes: a recording that
  carries one counts as decided and settles E2 and E3 negative. E1 with `afib` 0 and `afib_details`
  missing is undecided (no finding), since a detail of 1, 11 or 12 would fire it (ADR 0006 §11 OR).

**The recompute** (`derived/ecg_recompute.py`, kind `ecg_recompute`, singleton `ecg_recompute`,
input `{connection_id, origin: "backfill"}`). A row is current when its `estimated` has the running
`algo_version` and the row's `input_fingerprint`; every other row is read, analysed outside the
transaction and written only if it still has the same `signalid`, payload hash and sampling rate.
A rewrite or a purge in between wins. The write touches `estimated` only: no revision bump, no
evaluation queued (§15). Queued after every `ecg` pass (a new row or a rewritten signal) and by the
nightly reconciliation of each connection: **the sweep** after a module version bump, so stale
estimates are recomputed within a day. Until then the overlay already treats a gate-module
difference as no gate result and keeps the result on an `hrv`-only difference. A stored frame that
no longer decodes gets the `signal_missing` estimate. Logs: counts and the pipeline version.

Known gaps (decided in #307; changes would be their own issues): the job is claimed on the
scheduler's lane (3), not the backfill lane (5): `queue_enqueue` maps only `withings_backfill` to
lane 5 (as for `derive_normalize`), and mapping more kinds is a change to a security-critical SQL
function, so it stays as is. The sweep runs only from the nightly reconciliation, so a module
version bump reaches stale estimates within 24 h; the overlay covers the gap (above). No sweep
runs at worker start.

Tests: `tests/analysis/rules/test_ecg.py` and `test_ecg_intervals.py` (every rule case, the
undecided days, origin, the switch, E6's provenance into the overlay; no database),
`tests/test_derived_ecg_findings_db.py` (ingest to findings: history without notification, A7
triggers, E4 and the sex, no re-evaluation after a recompute, stale, worker context) and
`tests/test_derived_ecg_recompute_db.py` (current and stale rows, no bump, no evaluation queued,
the sweep and an `hrv`-only bump, rewrite and purge races, worker context, the enqueue).
