# Finding rules v1: body composition and heart (ADR 0010)

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
| `core.py` | the two bands, confidence, severity, result types, the shared `trend_rule` |
| `episodes.py` | `origin`, `advance_simple`, `advance_persistent`, `reconcile` |
| `body.py` | B1, B2, B3, B4, B5, B6 |
| `heart.py` | C1, C1x, C2 |

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
- **C1x**: the run is taken backwards from `day` in the direction of `day`'s value; no band, so no
  policy and confidence is R only.
- **B3** has no policy window, so confidence is `high`. The day's ratio is the earliest morning
  group that holds both ECW and ICW, otherwise the median ratio of the day's groups that do.
- **B6**: the per-day policy check runs on stand-in daily values (one per day with a pair); the
  median is over the groups in the 90 days ending at `day`. Duplicates are keyed on
  `(grpid, position)`, the first in canonical order wins.
- **B1**: the gain is `Decimal` exact; the ECW check is against the finding band at the start day.

## Tests

`backend/tests/analysis/rules/`: bands and origin (`test_core_bands.py`), episodes and persistence
with a scripted condition (`test_core_episodes.py`), confidence and severity
(`test_core_confidence.py`), deterministic and order-independent, insufficient on sparse series
(`test_core_properties.py`), the false-episode rate at phi 0.6 (`test_core_rate.py`: 80 seeded
no-change years, about 13 s; 4 of 80 years open an episode with persistence and 47 of 80 would
without it, against the ADR's bound of 10 %), and the rules (`test_body*.py`, `test_heart.py`).
Fixtures are synthetic (`rules_builders.py`).
