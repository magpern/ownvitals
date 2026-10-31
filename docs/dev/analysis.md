# Analysis core: quality gates, daily values, minimum-data policy

`backend/src/ownvitals/analysis/` is the database-free core of the M4 pipeline (PLAN §7.1 steps
2 to 4 and the `data_quality` of step 6), pulled forward as a pure library. Wiring to
`observations`, `metric_daily` and jobs comes later in M4. The statistical methods of step 5
(baseline, EWMA, Theil-Sen, PELT) live in `analysis/methods/` and have their own document.

**Pure**: no database, web, Withings, job, egress, account, configuration, logging or HTTP-client
import (the list is under "Purity guard"), no I/O, no clock, no randomness. Inputs are plain frozen
dataclasses; every function is deterministic and does not depend on the order its input was
passed in. `tests/test_analysis_purity.py` enforces the import rule for `ownvitals.analysis` and
`ownvitals.ecg` (below).

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/analysis tests/test_analysis_purity.py
```

No database is needed for these tests.

## Modules

| Module | What |
|---|---|
| `quality.py` | `Reading`, `assess()` (the quality gates), the plausibility table `METRICS`, `real_value()`, `from_epoch_seconds()` |
| `daily.py` | `daily_values()`: one `DailyValue` per local day |
| `dataquality.py` | `summarise()`: the `DataQuality` every finding carries |
| `policy.py` | `Requirement`, `POLICIES`, `evaluate()` / `evaluate_metric()`: `Sufficient` or `InsufficientData` |

The flow, for one metric of one user:

```python
result = assess("weight", readings)             # every reading back, flagged
daily = daily_values(result.usable)             # one value per local day
verdict = evaluate_metric("weight", Purpose.TREND, daily, window_end=today, window_days=90)
if isinstance(verdict, InsufficientData):
    ...                                          # n, coverage, shortfalls: never a value or finding
else:
    verdict.data_quality                         # n, coverage, gaps, frequency for the finding
```

## Conventions

**Values are exact.** A `Reading.value` is a `Decimal` holding the real value
(`value_raw x 10^unit`, applied by `real_value(value_raw, unit)`); `float` and `int` are refused,
so a rounded value cannot get in. Every operation runs in the `EXACT` decimal context (60 digits,
trapping any rounding), so a result is exact or an error. Numbers that go on to numpy (the
methods) are converted at that boundary, not here. Converting to other physical units is the
normalization step's job and is not done here.

**Time.** `measured_at` is a timezone-aware instant (normalised to UTC); `source_tz` is the IANA
zone the reading was taken in (`measure_groups.source_tz`). Build instants from integer epoch
seconds with `from_epoch_seconds()` (a float is refused: a float just below a midnight can round
across it and change the day). The **day** a reading belongs to is its calendar date in its own
`source_tz` (`Reading.local_date`); DST, repeated hours, trips and readings around midnight need
no special case because the zone rules turn the instant into a local date and time. The time zone
database of the host is used (the `python:3.12-slim-bookworm` runtime image has it, and the
`tzdata` package is installed on Windows through psycopg).

**Order.** Readings are processed in a canonical order `(measured_at, grpid, value, source_tz,
attrib)`, so the result never depends on the order they arrive in.

## Quality gates (`assess`)

`assess(metric_key, readings)` returns **every** input reading with its flags; nothing is dropped.
`QualityResult.usable` is what later steps use.

| Flag | When | Used? |
|---|---|---|
| `unconfirmed` | `attrib == 1` (ambiguous household measure) | no, shown as "unconfirmed" |
| `implausible` | outside the metric's inclusive range in `METRICS` | no |
| `duplicate` | the same `grpid` again; the first in canonical order is the original | no (the original is) |
| `jump` | differs from an earlier usable reading within 24 h (inclusive) by more than `max(2 kg, 3 % of that earlier reading)` | **yes**, advisory only |

Decisions where PLAN §7.1 leaves room:

- Only identical `grpid`s are duplicates: deliberate repeat measurements, even at the same
  instant and value, are distinct readings and are all kept.
- A jump is judged only among readings that pass the other gates, so a household member's
  55 kg `attrib=1` reading or a broken scale cannot make real readings look like jumps. Only the
  later reading of a pair is flagged; the reference for the 3 % is the earlier reading. After an
  isolated outlier the next reading is flagged too (it jumps back); both flags are advisory.
- Only weight has a jump rule (the spec names kilograms). Other metrics have none.
- A metric without a row in `METRICS` is refused (`UnknownMetricError`): add its range and
  source first. Unknown meastypes are still stored; they are just not analysed.

### Plausibility ranges

Inclusive. An "engineering bound" is ours: wide enough that no real reading falls outside, so only
a broken scale, a unit mix-up or a typo is excluded; it is not a clinical statement. A range with a
document source cites it. Units are the ones shown (`value x 10^unit`). Basal metabolic rate (226)
has no row because its unit is not documented in the findings.

| Key | Meastype | Unit | Range | Source |
|---|---|---|---|---|
| `weight` | 1 | kg | 10 to 300 | engineering bound |
| `fat_free_mass` | 5 | kg | 5 to 250 | engineering bound |
| `fat_ratio` | 6 | % | 1 to 75 | engineering bound |
| `fat_mass` | 8 | kg | 0.5 to 250 | engineering bound |
| `diastolic_bp` | 9 | mmHg | 20 to 200 | engineering bound |
| `systolic_bp` | 10 | mmHg | 40 to 300 | engineering bound |
| `heart_pulse` | 11 | bpm | 20 to 250 | engineering bound |
| `spo2` | 54 | % | 50 to 100 | engineering bound |
| `muscle_mass` | 76 | kg | 5 to 200 | engineering bound |
| `hydration` | 77 | kg | 5 to 150 | engineering bound |
| `bone_mass` | 88 | kg | 0.3 to 10 | engineering bound |
| `pulse_wave_velocity` | 91 | m/s | 1 to 30 | engineering bound |
| `qrs_duration` | 135 | ms | 20 to 300 | engineering bound |
| `pr_interval` | 136 | ms | 40 to 500 | engineering bound |
| `qt_interval` | 137 | ms | 100 to 800 | engineering bound |
| `qtc_interval` | 138 | ms | 200 to 800 | engineering bound |
| `vascular_age` | 155 | years | 10 to 120 | engineering bound |
| `nerve_health_score` | 167 | score | 0 to 100 | findings §3 (NeuropathyFeature score range 0-100) |
| `extracellular_water` | 168 | kg | 1 to 50 | engineering bound |
| `intracellular_water` | 169 | kg | 2 to 100 | engineering bound |
| `visceral_fat` | 170 | index | 0 to 100 | engineering bound |
| `nerve_response_score` | 196 | score | 0 to 100 | findings §3 (NeuropathyFeature score range 0-100) |
| `metabolic_age` | 227 | years | 5 to 150 | engineering bound |
| `esc` | 229 | µS | 0 to 200 | engineering bound |

The metric keys are the ones the normalization step must use for `observations.metric_key`. A
test keeps the table consistent (unique meastypes, `low < high`, every range cites a source or is
an engineering bound).

## Daily value (`daily_values`)

Pass `QualityResult.usable`; a repeated `grpid` is a `ValueError`. Per local day:

- **Morning**: the earliest reading (by instant) whose local time in its own zone is in
  `[04:00, 11:00)` (04:00:00 is in, 11:00:00 is out). Its value is the daily value.
- **Median**: otherwise the median of the day's readings (an even count averages the middle two,
  exactly).
- `n`, `minimum`, `maximum` and `mad` (the raw, unscaled median absolute deviation) always cover
  **all** the day's readings, not only the morning ones.
- Days with no reading have no entry; gaps are `DataQuality`'s business.

## Data quality (`summarise`)

For the window of `window_days` calendar days ending on `window_end` (both ends inclusive):
`n_readings`, `n_days` (days with data), `coverage = n_days / window_days`, `span_days`,
`span_fraction = span_days / window_days`, `gaps` (runs of consecutive days without data of at
least `min_gap_days`, default 3, including at the window's edges), `longest_gap_days` (over every
run), `readings_per_week` and `median_days_between`. Ratios are exact `Fraction`s; `as_dict()`
gives a JSON-safe form for `findings.data_quality`.

**A span is days elapsed.** `span_days = (last_day - first_day).days`: data on 1 June and 29 June
is a span of 28 days (four weeks), and one day with data is a span of 0. "28 days" in a
requirement therefore means 28 days elapsed, never 27 (until `dataquality/2` the span counted both
end days, so 27 days elapsed passed as 28). Every span in the module follows this: the
`min_span_days` thresholds, and `span_fraction`, which is days elapsed over the window's length in
days (`window_days`), so a window can never be spanned completely: first to last day of a 30-day
window is 29 days, a fraction of 29/30. Counts of calendar days are not spans and stay counts: the
window (`window_days`), a gap (`Gap.days`, two missing days are a gap of 2) and
`longest_gap_days`.

## Minimum-data policy (`policy.py`)

Declarative, per metric and purpose. A "point" is a day with data. Every threshold is inclusive.

| Requirement | Days with data | Span | Where |
|---|---|---|---|
| trend (default) | at least 8 | at least 60 % of the window (`span_days / window_days >= 3/5`, exact: 18 days elapsed in a 30-day window) | PLAN §7.1 step 4 |
| baseline (default) | at least 10 | at least 21 days elapsed | PLAN §7.1 step 4 |
| trend, nerve metrics (`nerve_health_score`, `nerve_response_score`, `esc`) | at least 4 | at least 28 days elapsed | N1, PLAN §7.2 |
| baseline, nerve metrics (same three) | at least 10 | at least 28 days elapsed | derived from N1 (below) |

The nerve baseline has its own requirement (`NERVE_BASELINE`) so that it is never easier to meet
than N1: with the default baseline's 21 days, ten measures over three weeks would give a
sufficient baseline while the trend still says "insufficient data". A test checks, over a grid of
days with data and spans, that a nerve baseline is never sufficient while N1 is not.

Below a requirement the result is `InsufficientData` (`requirement`, `shortfalls`, `n`,
`coverage`, the whole `data_quality`); at or above it, `Sufficient` with the same
`data_quality`. Neither is a value or a finding. To change a metric's thresholds, add it to
`POLICIES` and test the new boundaries (at, just below, just above).

**A `Requirement` that can never be met is refused.** `min_span_fraction` must be in the open
interval (0, 1): a span is days elapsed, at most `window_days - 1`, so no window gives a
`span_fraction` of 1 or more (`(w - 1) / w` at most), and a requirement of 1 would make every
result `InsufficientData` whatever the data. A fraction below 1 that a *short* window cannot reach
(3/5 of a 2-day window) is valid: that window is `InsufficientData`, like a `min_span_days` larger
than the window, and the same requirement is met by a longer one.

## Provenance: `ALGO_VERSION`

Each module has an `ALGO_VERSION` (`quality/1`, `daily/1`, `dataquality/2`, `policy/2`) and
every result carries it (PLAN §5: derived rows store `algo_version`). **Bump the module's number
whenever its behaviour changes** (a threshold, a window, a rule, the canonical order), in the same
change as the tests; derived rows computed with the old number then show as stale.

## Tests

`backend/tests/analysis/` holds the unit tests per module and the end-to-end tests. Fixtures are
**synthetic** (AGENTS.md): `synthetic_readings.make_series(seed)` is a seeded generator of
irregular weight series with gaps, clustered repeats, trips across time zones (New York, Tokyo),
both 2026 European DST days, `attrib=1` readings, jumps, implausible readings and re-delivered
duplicates, and returns what it planted (`truth`). Named tests cover the morning window (both
edges), the median fallback, DST (spring forward, fall back, the repeated hour, a 30-minute
change), crossing midnight (to the microsecond, in seven zones), travel, and the day assignment
against exact integer arithmetic. Each policy threshold is tested at, just below and just above its
boundary, in days elapsed: some of those tests build the days with explicit dates, not with the
`spread_days` helper, so a helper and a rule cannot be wrong in the same way.

## Purity guard

`tests/test_analysis_purity.py` fails when `ownvitals.analysis` (the `methods/` subpackage
included) or `ownvitals.ecg` (once it exists) imports anything in `FORBIDDEN`:

| Forbidden | Why |
|---|---|
| `ownvitals.db`, `ownvitals.jobs`, `ownvitals.connections`, `sqlalchemy`, `psycopg` | the database |
| `ownvitals.web`, `ownvitals.admin`, `ownvitals.flags`, `ownvitals.headers` | the web app and its routes |
| `ownvitals.withings`, `ownvitals.egress`, `httpx`, `requests` | the network |
| `ownvitals.auth`, `ownvitals.context` | who is acting: the pure core takes plain values |
| `ownvitals.config`, `ownvitals.log`, `ownvitals.cli` | the environment, secret files, stdout and the operator CLI |
| `ownvitals.__main__` | the entry point: it starts uvicorn and imports the web app, config and logging |

Everything else in `ownvitals` stays importable (`ownvitals.emailnorm` is the one pure helper at
the top level). A name matches itself and its submodules, not a name that merely starts with it
(`ownvitals.authors` is not `ownvitals.auth`). When a package with I/O merges, add it to
`FORBIDDEN`; `ls backend/src/ownvitals/` shows what is there.

- **static**: every import in the package's source is read from the syntax tree (absolute and
  relative imports, `from ownvitals import db`, `import ownvitals` then `ownvitals.db`,
  `importlib.import_module` and `__import__`). The scanner fails closed, so it also reports:
  - an import call whose target is not a string literal, or starts with `.` (a relative target
    such as `'.db'` or `'..db'` is resolved against a package the scanner cannot see);
  - an import call with a second positional argument, `package=`, `level=` or `**kwargs`;
  - an import call whose `fromlist` names a forbidden module (`__import__('ownvitals',
    fromlist=['db'])`) or is not a literal;
  - **call chaining**: a call is an import call by the name it is made under, whatever its base,
    so `__import__('importlib').import_module('ownvitals.db')` and
    `sys.modules['importlib'].import_module(...)` are checked like `importlib.import_module`;
  - any use of `exec`, `eval` or `compile` (also as `builtins.exec`, and on a base that is not a
    plain name: `__import__('builtins').exec(...)`), which can run an import that no syntax tree
    shows (`re.compile` is not the builtin and is fine);
  - any use of `run_module`, `run_path`, `resolve_name` or `patch` (`runpy`, `pkgutil` and
    `unittest.mock`, also `patch.dict` and `patch.multiple`), which import or run the module a
    string names, and for which a pure library has no use;
  - a string that is exactly the name of one of those functions (`getattr(importlib,
    'import_module')`, `importlib.__dict__['import_module']`), which can only be a lookup by name;
    the names are `import_module`, `__import__`, `exec`, `eval`, `compile`, `run_module`,
    `run_path`, `resolve_name` and `patch`;
  - `import_module` or `__import__` used as a value or under another name
    (`from importlib import import_module as load`, `load = importlib.import_module`);
  - **aliases are collected from the whole file first**, so a use before (or above, in the walk's
    depth order) an `import ownvitals` nested in a function or class is caught, and a name bound
    by two imports (`import ownvitals as x`, `import os as x`) stands for both;
- **words to avoid**: whatever the word means in the code, `patch`, `run_module`, `run_path` and
  `resolve_name` are refused as an identifier (`patch = ...`, `sample.patch`), `exec`, `eval` and
  `compile` as a plain name (`re.compile(...)` stays allowed), and all nine names above as an
  exact string (`'patch'` as a dictionary key or a label). Code in `ownvitals.analysis` and
  `ownvitals.ecg` uses other words, for example `electrode` for a skin patch;
- **dynamic**: importing every module in a clean interpreter must not load any forbidden module,
  which also catches an indirect import. A name built at run time (`getattr(m, 'import_' +
  'module')`) is beyond a syntax tree and is left to this check;
- the scanner is tested by planting each of those forms into a copy of the package, once in
  `quality.py` and once in `methods/trend.py`, and the report must name the rule that catches it
  (`tests/analysis/test_analysis_purity_rules.py`; each `FORBIDDEN` addition is listed there by
  name, so removing an entry fails a test);
- **no silent skip**: a guarded name is skipped only while neither `ownvitals/<name>/` nor
  `ownvitals/<name>.py` exists. If `ecg/` exists its checks run; if `ecg.py` exists the guard
  **fails**, because the ECG core (#40) must be a package that the scan can walk
  (`tests/analysis/test_analysis_purity_guarded.py`).

To widen the list, edit `FORBIDDEN` there; to guard a new pure package, add it to `GUARDED`.
