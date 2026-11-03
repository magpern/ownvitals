# Finding rules v1: body, heart, nerve and system (ADR 0010)

`backend/src/ownvitals/analysis/rules/` holds the pure rule functions of
[ADR 0010](../adr/0010-finding-rules-v1.md) (test specification:
[0010-finding-rules-v1-tests.md](../adr/0010-finding-rules-v1-tests.md)). Same contract as the rest
of `ownvitals.analysis` ([analysis.md](analysis.md)): no database, network, clock or randomness; the
purity guard scans the package. `today` and the evaluation `day` are arguments.

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/analysis/rules tests/test_analysis_purity.py
```

| Module | What |
|---|---|
| `core.py` | the two bands, confidence, severity, result types, the registry |
| `trendrule.py` | `trend_of`, `relative_change` and the shared `trend_rule` |
| `episodes.py` | `origin`, `advance_simple`, `advance_persistent`, `reconcile` |
| `body.py` | B1, B2, B3, B4, B5, B6 |
| `heart.py` | C1, C1x, C2 |
| `nerve.py` | N1 (the gate), N2, N3, N5 |
| `system.py` | Q1's four parts |

## Calling a rule

A rule is evaluated on one **evaluation day** that has its trigger metric's daily value (a day
without one is a `ValueError`: the caller schedules evaluations, ADR §1).

```python
result = b2(weight_daily, day, provenance={"weight": {day: Provenance(groups=(12,), jump=False)}})
```

`daily` is `daily_values(assess(...).usable)` (#37). `provenance` is optional: per metric key and
day, the measure groups a daily value came from (the trigger groups of the finding's evidence) and
whether a trigger reading carried #37's advisory `jump` flag (criterion J). The wiring issue fills
it. A rule returns one of:

| Result | Means |
|---|---|
| `Finding` | the rule fired: rule id and version, tier, subject, window, `reference_day`, confidence, severity, `evidence` (including `trigger_groups`), `data_quality` |
| `NoFinding` | the rule was evaluated and did not fire |
| `Insufficient` | a policy failed, or the rule is not available (B3 without ICW): never a finding; `shortfalls` and `data_quality` say why |

The tier is carried, never decided: B1, B3 and C1x are `experimental`, and who may see them (the
owner-only flag) is the caller's.

### Signatures

| Rule | Call | Subject |
|---|---|---|
| B1 | `b1(weight, ecw, day, provenance=)` (daily values of 1 and 168) | `weight` |
| B2 | `b2(weight, day, provenance=)` | `weight` |
| B3 | `b3(ecw, icw, day)` (usable `Reading`s of 168 and 169, paired by `grpid`) | `water_ratio` |
| B4 | `b4(muscle_mass, day, provenance=)` | `muscle_mass` |
| B5 | `b5(daily, day, metric_key=, provenance=)` | `visceral_fat` or `metabolic_age` |
| B6 | `b6(segments, day, pair="arms" or "legs")` (`Segment(position, reading)` of type 175) | the pair |
| C1 | `c1(heart_pulse, day, provenance=)` | `heart_pulse` |
| C1x | `c1x(heart_pulse, day, provenance=)` (own `rule_id`, experimental) | `heart_pulse` |
| C2 | `c2(vascular_age, day, provenance=)` (no birth year) | `vascular_age` |

## Bands (ADR §2)

`display_band` (k 1.5; `TRAILING` is `q-89 … q`, `PRECEDING` is `q-90 … q-1`) and `finding_band`
(k 2.5, the 90 days before the reference day) are `median ± max(w, k · 1.4826 · MAD)`, gated by
the metric's BASELINE policy (`InsufficientData` below it). `w` is `MIN_HALF_WIDTH` (the mock-ups'
`bandMin`, otherwise one display step); a metric without an entry is a `KeyError`: add it with its
source. Every call passes `k`: `baseline.DEFAULT_K` is nobody's band, and a test scans the package
for a `rolling_baseline` call without `k=`. A value equal to a threshold does not fire ("above" is
strict); on exact `Decimal`/`Fraction` rules (B1's kilograms, B3's ratio, B6's index) the
comparison is exact.

## Episodes (ADR §4)

`origin(window_end, today)` is `live` through 6 days back. Two pure state transitions turn rule
results into episodes; the wiring issue stores them:

- `advance_simple(episode, result, today=)` for every rule that is not a trend test: the first
  firing opens, a firing updates (the window grows; `reference_day`, `first_seen` and `origin`
  stay), the first non-firing or insufficient evaluation ends it. C1, C1x, B1, B3 and B6 use it.
- `advance_persistent(state, result, today=)` for the rules whose condition includes a `trend`
  test (B2, B4, B5, C2; `SPECS[rule].persistent`): the `Finding` is the day's condition; an episode
  opens when the true run spans 21 calendar days (first and last included) with at least 6
  evaluations. A false or insufficient evaluation breaks a run. An open episode ends at a failing
  evaluation when every evaluation in the 7 calendar days ending on it failed (its last true
  evaluation was 7 or more days earlier). Call it only for evaluation days, in order.
- `reconcile(previous, recomputed)` keeps `first_seen` and `origin` when a recompute has the same
  rule, subject and reference day. `Step.notify` is true only for the first write of a live
  episode.

The `reference_day` of a single-day rule (B3, B6) and of a trend condition is the evaluation day;
an episode of a trend-test rule takes the run's first day.

## Rules, and decisions where the ADR leaves room

- **Trend rules** (`trend_rule`): `Series.from_daily` (never `from_instants`), `trend` with the
  AR(1) correction at alpha 0.05 over the window ending at `day`, plus the effect threshold
  `slope · days / intercept` (B2: at most -0.05 over 182 days; B4: at most -0.02 over 90). Effects
  are reported in metric units or as that ratio, never in noise units. Confidence is the lowest of
  D (twice the policy's days with data), T (low on under 20 points with phi above 0.5, #87; high
  when p < alpha/5) and, for weight, J.
- **C1**: of the runs ending at `day`, the longest one that satisfies the condition against the
  band at its own first day wins, so a growing run keeps its first day and band. The band is
  the display band (k 1.5) `PRECEDING` that first day. A run may begin on an already raised day
  that its own band calls raised.
- **Band memo** (#187): `c1` and `n2_routine` take an optional `bands=` dict, a memo of the band
  at each run's first day for a caller that evaluates many days of the same series (the rule
  evaluation's replay); the result is the same with or without it (tested).
- **C1x**: the run is taken backwards from `day` in the direction of `day`'s value; no band, so no
  policy and confidence is R only.
- **B3** has no policy window, so confidence is `high`. The day's ratio is the earliest morning
  group that holds both ECW and ICW, otherwise the median ratio of the day's groups that do.
- **B6**: the per-day policy check runs on stand-in daily values (one per day with a pair); the
  median is over the groups in the 90 days ending at `day`. Duplicates are keyed on
  `(grpid, position)`, the first in canonical order wins.
- **B1**: the gain is `Decimal` exact; the ECW check is against the finding band at the start day.

## Nerve health and system rules (#184)

`nerve.py` (nerve keys `nerve_health_score` 167, `nerve_response_score` 196, `esc` 229; any other
key is a `ValueError`, and the three are never mixed) and `system.py`. Same results and contract as
above.

| Rule | Call | Rule id and subject |
|---|---|---|
| N1 | `n1(key, daily, day, window_days=90)`: `NERVE_TREND` (4 days with data spanning 28) as `Sufficient` or `InsufficientData`; never a finding | none |
| N2 guided | `n2_guided(readings, day, metric_key=)` | `N2:guided`, the key |
| N2 routine | `n2_routine(readings, day, metric_key=, provenance=)` | `N2:routine`, the key |
| N3 | `n3(daily, day, metric_key=, provenance=)` | `N3`, the key |
| N5 | `n5(first, second, day, pair="158_159" or "197_198")` (usable `Reading`s of the two members) | `N5`, `nerve_foot_<pair>` |
| Q1 | `q1_no_weigh_in(weight, capability, today)`, `q1_low_battery(DeviceState, today)`, `q1_reconnect(ConnectionState, today)`, `q1_import_incomplete(StreamState, today)` | `Q1:no_weigh_in` (`user`), `Q1:low_battery` (`hash_deviceid`), `Q1:reconnect` (`connection`), `Q1:import_incomplete` (the stream) |

Decisions where the ADR leaves room:

- **N2's two parts have their own rule ids** (`N2:guided`, `N2:routine`, `SPECS`/`SEVERITY`), so a
  guided and a routine finding of one key are separate episodes (§4: one active per rule id and
  subject). Q1 has one rule id per part for the same reason and because severities differ. The
  wiring issue stores these ids.
- **N2 takes the metric's usable `Reading`s** (guided `attrib` 15 and routine alike): the band and
  N1 read the daily values of all of them, the guided part reads the readings, the routine part
  the daily values of the `attrib` other than 15 readings. Guided: the first guided reading below
  `finding_band(day).low` fires (strictly below), window and reference day `day`. Routine: of the
  routine measure days ending at `day`, the longest run of at least 2 with every value below the
  band at the run's first day (like C1); a run's baseline gap is skipped, the shortest run's is
  insufficient data. Confidence: D (N1 and the band), and R with a minimum of 2 (high from 4).
  Both parts raise `ValueError` when `day` has no trigger reading (the caller schedules).
  Neither is persistent: they open on the first firing and end on the first non-firing evaluation.
- **N3** tests the 180 then the 90 days at alpha 0.025 each; a window below N1 is not tested; both
  below is insufficient data. The window is the 180 if it fired. `persistent` (`advance_persistent`):
  a weekly cadence opens on its 6th evaluation (day 36).
- **N5** pairs readings by `grpid` (the first duplicate wins), the days with a pair are checked
  with `NERVE_TREND`, the median SI is exact and "more than 0.20". No group with both members
  anywhere is `Insufficient` with a reason starting "not supported", never "no difference".
  The foot keys `nerve_foot_158` and so on need no metric rows: no daily value or band is built.
- **Q1** inputs are plain dataclasses (`DeviceState`, `ConnectionState`, `StreamState`); capability
  strings are PLAN §4.2's and only `"supported"` counts. No weigh-in takes the usable weight
  readings (an `attrib` 1 reading is ignored even if passed); its reference day is the last
  weigh-in, so the episode is stable. Findings carry the state they read in `evidence["state"]`
  and `data_quality`; confidence is `high`, tier `system`, severity per part (§6).

## Not in this module

ADR 0010 test-spec "Episodes and origin" case 6 (`today` follows the `source_tz` of the user's
latest reading, or the fixed zone) belongs to the wiring issue #187: here `today` is an argument.

## Tests

`backend/tests/analysis/rules/`: bands and origin (`test_core_bands.py`), episodes and persistence
with a scripted condition (`test_core_episodes.py`), confidence and severity
(`test_core_confidence.py`), deterministic and order-independent, insufficient on sparse series
(`test_core_properties.py`), the false-episode rate at phi 0.6 (`test_core_rate.py`: 80 seeded
no-change years, about 13 s; 4 of 80 years open an episode with persistence and 47 of 80 would
without it, against the ADR's bound of 10 %), and the rules (`test_body*.py`, `test_heart.py`, `test_nerve*.py`, `test_system.py`).
Fixtures are synthetic (`rules_builders.py`).
