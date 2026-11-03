# Derived data: schema

The tables the analysis writes (PLAN §5 and §7, roadmap Amendments 2 and 3, ADR 0010 §4,
ADR 0002 §2). Migration `0011_analysis_schema` creates them; `ownvitals.derived.tables` holds the
Core tables and the vocabularies the migration's check constraints accept. Writers come with the
M4 wiring issues; this is the schema only (#181).

All five tables are **owned**: `owner_id` cascading from `users`, forced RLS with the
`owner_isolation` policy, `UNIQUE (owner_id, id)` (or the partitioned key below), and every other
unique key includes `owner_id`. No foreign key between them and none into `observations`.

| Table | Key | What it holds |
|---|---|---|
| `observations` | `PRIMARY KEY (owner_id, id, ts)`; monthly partitions on `ts`; BRIN on `ts` | One normalised reading: `ts`, `source_ts`, `source_tz`, `value`, `source_value_raw`, `source_unit`, `conversion`, `quality_flags` (`unconfirmed`, `implausible`, `duplicate`, `jump`), and `source_ref` as three columns `source_grpid`, `source_type`, `source_position` |
| `metric_daily` | `UNIQUE (owner_id, metric_key, day)` | #37's `DailyValue`: `method` (`morning`/`median`), `value`, `morning` (set exactly when the method is `morning`, and equal to `value`), `mean`, `min`, `max`, `mad`, `n`, and `source_groups` (the grpids the value came from: one for `morning`, every group the median used otherwise; ADR 0006 §11) |
| `findings` | partial unique `findings_active_episode (owner_id, rule_id, subject) WHERE status = 'active'` | PLAN §5's columns; `window` is `window_start`/`window_end`; `subject`, `status` (`active`/`ended`, `ended_at` set exactly when ended), `reference_day`, `notify_state` (`none`/`pending`/`held`/`sent`), `notified_at`, `seen_at` |
| `scores` | `UNIQUE (owner_id, kind, day)` | ADR 0012: `kind` is `his`, `readiness` or `body_path:<metric_key>`; `components.status` is `computed`, `insufficient_data`, `not_available`, `not_supported` or `not_set`, and `value` is set exactly when it is `computed` |
| `user_profiles` | `UNIQUE (owner_id)` | The Settings mock-up: birth year, sex, day assignment (`reading_tz`, or `fixed_tz` with `day_tz`), display and notification preferences |

Provenance (PLAN §5): `observations`, `metric_daily` and `scores` carry `algo_version`
(`name/N`, as the analysis modules spell it), `input_revision` and `input_fingerprint`
(32 bytes); `findings` carries `rule_version`, `input_revision` and `input_fingerprint`.

`input_revision` is one `bigint`. A row that reads one stream stores that stream's
`data_revisions.revision`. A row that reads several (a score, a finding over two metrics; ADR
0012 §1 says "the counters of every stream read") stores **the sum of the counters of every
stream it read**. Each counter only grows and is bumped on every change of its stream, so any
change to any input stream changes the sum. Removing a stream's counter row (the "delete
imported data" path) can lower a sum back to an earlier value, but it also changes the input
rows, so `input_fingerprint` differs and the row is stale anyway. A reader therefore always
compares **both** values. The writer issue implements the sum in one place and tests it.

**Metric keys** (`observations.metric_key`, `metric_daily.metric_key`, each element of
`findings.metric_keys`, and the part after `body_path:` in a score kind) match
`^[a-z][a-z0-9_]{0,39}(:[a-z][a-z0-9_]{0,23})?$` (`derived.tables.METRIC_KEY_PATTERN`): a
lower-case name of letters, digits and underscores, then optionally one `:` and a second part
of the same alphabet. The second part is ADR 0010's segment or side, for example
`segmental_muscle_mass:left_arm`. Upper case, a second colon or an empty part are refused.
`findings.metric_keys` holds up to 16 such keys and may be empty (a Q1 finding has no metric);
every element is checked on its own: an empty element, a NULL element or an element holding a
comma is refused (the check matches the comma-joined string and requires as many parts as
elements).

**NULL-proof checks.** A `CHECK` that evaluates to NULL passes in Postgres. Every check that
reads a JSON key or a nullable value is written so it cannot: `scores_status` and
`findings_metric_keys` are wrapped in `coalesce(..., false)`, a score's status must be a JSON
string (`jsonb_typeof(components->'status') = 'string'`), and "value only when computed" uses
`IS NOT DISTINCT FROM`. Nullable columns a check reads (`metric_daily.morning`,
`observations.source_*`, `findings.ended_at` and `notified_at`, `user_profiles.day_tz`) are
tested with `IS NULL`/`IS NOT NULL`, never compared with `=`. Array columns refuse NULL
elements with `array_position(col, NULL) IS NULL`. `birth_year`, `sex` and `scores.value` may
be NULL by design (unset, or a score that is not `computed`).

Constraints worth knowing before writing a row:

- `tier = 'system'` exactly when `rule_id = 'Q1'` (Amendment 3); device and system findings are
  `confidence = 'high'` (ADR 0010 §5).
- A `backfill` finding has `notify_state = 'none'` (PLAN §7.1 step 7); `sent` exactly when
  `notified_at` is set.
- An evaluation that fires while an episode is active **updates** that row; inserting a second
  active row for the same (owner, rule, subject) is refused. End it (`status = 'ended'`,
  `ended_at`) before opening a new one.
- Numbers are finite and within ±10¹²; `NaN` and the infinities are refused.
- `observations` deduplicates on `(owner_id, metric_key, source_grpid, source_type,
  source_position, ts)` with `NULLS NOT DISTINCT`. Segmental types 173–175 repeat in one group
  with different positions, so they stay distinct; a deliberate repeat measurement has its own
  grpid and stays too.

## Partitions

`ensure_month_partitions` (`docker/postgres/withings/40-withings.sql`) admits `observations`
beside `sleep_series` and `intraday`; `ownvitals.ingest.partitions.ensure_month` is the only
caller, one short transaction per month, before the insert (ADR 0002 §2 rule 6). Changing the
function's allowlist changed its pinned digest (`ownvitals/db/queue_digests.py`).

**Not done yet:** PLAN §5's "created 3 months ahead by the scheduler" needs a scheduler step
that does not exist on `main`. Until it lands, an `observations` month exists only once a writer
has asked for it. That step calls `ensure_month_partitions` for every table in
`PARTITIONED`, `observations` included.

## "Delete imported data"

ADR 0004 §7's "also delete imported data" option (`connections/purge.py` `IMPORTED`) removes
`observations`, `metric_daily`, `findings` and `scores` with the imported rows (#218): they are
derived from them and are health data. Like the imported rows they are per owner, not per
connection, so the option removes all of the user's derived rows. `user_profiles` stays
(settings, listed in `KEPT`). Account deletion removes all five through the `owner_id` cascade.
`tests/test_withings_disconnect_account.py` enumerates every owned table against `IMPORTED` and
`KEPT`, so a new owned table must join one of them.

## Tests

`tests/test_derived_schema.py` (guard 1 on the tables and their partitions, the episode index,
the system tier, the check constraints, the vocabularies, the round trip) and
`tests/test_derived_schema_rls.py` (two users in the web-request and worker-job contexts of
`ctx.py`, with and without the repository's own owner filter; no user, no rows; no row written or
moved to another owner). Each table also has its row in `tests/contexttools.py` `ROWS`, so the
six-context probe of [isolation.md](isolation.md) covers it. An owner has one profile, so for
`user_profiles` the probe's own-insert control first deletes the context's own profile in the same savepoint
(`CLEAR_BEFORE_OWN_INSERT`), so the foreign insert stays a plain insert that only the policy can
refuse; `user_profiles`, `observations` and `findings` are in the relaxation matrix
(`MATRIX_TABLES`). They need the test database of [backend.md](backend.md).
