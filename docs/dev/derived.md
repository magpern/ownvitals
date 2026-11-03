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
| `scores` | `UNIQUE (owner_id, kind, day)` | `value`, `components` |
| `user_profiles` | `UNIQUE (owner_id)` | The Settings mock-up: birth year, sex, day assignment (`reading_tz`, or `fixed_tz` with `day_tz`), display and notification preferences |

Provenance (PLAN §5): `observations`, `metric_daily` and `scores` carry `algo_version`
(`name/N`, as the analysis modules spell it), `input_revision` and `input_fingerprint`
(32 bytes); `findings` carries `rule_version`, `input_revision` and `input_fingerprint`.

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
function's allowlist changed its pinned digest (`ownvitals/db/queue_digests.py`). The scheduler's
"3 months ahead" step does not exist yet; it calls the same function for every partitioned table
when it lands.

## Tests

`tests/test_derived_schema.py` (guard 1 on the tables and their partitions, the episode index,
the system tier, the check constraints, the vocabularies, the round trip) and
`tests/test_derived_schema_rls.py` (two users in the web-request and worker-job contexts of
`ctx.py`, with and without the repository's own owner filter; no user, no rows; no row written or
moved to another owner). Each table also has its row in `tests/contexttools.py` `ROWS`, so the
six-context probe of [isolation.md](isolation.md) covers it; `user_profiles`' row is an upsert on
`owner_id`, because an owner has one profile and the probe's own-insert control inserts a second
time. They need the test database of [backend.md](backend.md).
