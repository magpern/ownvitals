# Derived data: schema

The tables the analysis writes (PLAN §5 and §7, roadmap Amendments 2 and 3, ADR 0010 §4,
ADR 0002 §2). Migration `0011_analysis_schema` creates them; `ownvitals.derived.tables` holds the
Core tables and the vocabularies the migration's check constraints accept. Writers come with the
M4 wiring issues; this file starts with the schema (#181) and goes on with the writers:
normalisation into `observations` (#185) and the daily values in `metric_daily` (#186).

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
(settings), and so does `mail_outbox` (notification state, no health data); both are listed in
`KEPT`. Account deletion removes all five derived tables through the `owner_id` cascade.
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

## Normalisation: `observations` (#185)

PLAN §7.1 step 1. Code: `derived/metric_keys.py` (the key table), `derived/normalize.py` (pure:
`normalise()`, `fingerprint()`), `derived/normalize_db.py` (read, verify, write) and
`derived/normalize_job.py` (the `derive_normalize` kind and `enqueue_normalize`).

**Keys.** `metric_key(meastype, position)`: the keys of `analysis.quality.METRICS` for the types
the analysis judges; `height`, the three temperatures, `vo2_max`, the two AFib enums and
`basal_metabolic_rate` besides; `segmental_fat_free_mass`, `segmental_fat_mass` and
`segmental_muscle_mass` for 173 to 175 with `:<position>` (`left_arm`, `right_arm`, `right_foot`,
`left_foot`, `left_body`, `right_body`, `left_leg`, `right_leg`, `torso`; other positions
`position_<n>`, none `unpositioned`); **every other type is kept** as `meastype_<n>`. A test pins
the table against the fake's meastypes.

**Row.** `value = value_raw x 10^unit` exactly (`Decimal`); `source_value_raw`, `source_unit` and
`conversion` (`e<unit>:<physical unit>`, for example `e-3:kg`; the unit is `unknown` where the
findings document none, and no value is converted to another unit) are kept. `ts` and `source_ts`
are the group's instant and `source_tz` its zone. `source_grpid`, `source_type`, `source_position`
are the source reference. Only groups with `category` 1 or none are readings (2 is the user's
objective); a soft-deleted group yields no rows. The quality flags are #37's `assess()` for a
metric with a row in its table, `unconfirmed` (`attrib = 1`) for any other. Nothing is dropped
for a flag. Rows the table cannot hold (a value beyond ±10^12, a unit outside ±30, an unknown
zone, an instant outside the 2009 to `now + 3 months` partition range) and a second copy of the
same `(metric, group, type, position)` are counted (`Skipped`), never written and never logged
with a value.

**Provenance.** `algo_version = normalize/1`; `input_revision` is the owner's `measures`
`data_revisions` counter; `input_fingerprint` is SHA-256 over the algorithm versions and the sorted
`(group id, payload hash, deleted mark)` of the owner's reading groups, deleted ones marked
(`fingerprint()`). Bumping `normalize/N` or `quality/N` changes every row.

**A pass** (`normalise_owner`, one owner, all connections): (1) read the revision, then the
groups and measures; if every stored row already carries this revision, fingerprint and algorithm
version, stop (`current`); (2) compute, and create the missing months with `ensure_month`, one
committed transaction per month; (3) in one transaction **lock the revision row `FOR SHARE`, check
that it and the groups' fingerprint are the ones read, and only then write** by natural key
(`observations_source_key`): insert what is missing, update what changed, delete what has no
source any more (a soft-deleted group, a backdated instant), and move the provenance of the rest
with one statement. Running twice gives identical rows with identical ids.

**Stale inputs write nothing.** The check and the write share one transaction, and the lock makes
an ingest bump or the purge of imported data wait for it, so a pass cannot write rows computed
from data a purge removed between its read and its write (issue #187's comment, from the review of
#225). A moved revision or fingerprint gives `stale`; a missing revision row (nothing imported, or
purged) gives `nothing`. Whatever moved the inputs also queues a new pass, so `stale` needs no
retry. The write holds the lock until it commits, so an ingest of the same owner can wait for it
(the role's 5 s `lock_timeout` applies); a first pass over a long history is the slowest case.
Later writers (#186, #187) follow the same rule.

**The job.** `derive_normalize`, input `{connection_id, origin: live|backfill}` (the connection
only finds the owner for `queue_enqueue`; the pass covers the whole owner), registered in the
worker's registry only. It is queued, through `queue_enqueue`, after the measures backfill
(`backfill`), the measures incremental sync and the nightly reconciliation (`live`). The two
origins have separate singleton keys (`normalize_live`, `normalize_backfill`), so one never
collapses into or delays the other; repeats of one origin collapse as `withings_sync` does. The
job runs as the owner under the fence (`job.fenced()`), so RLS applies (worker-context test).

**Known gaps.**

- The claim lane of `derive_normalize` is the scheduler's (3) for both origins: `queue_enqueue`
  maps only `withings_backfill` to the backfill lane (5), and changing it is a change to
  `docker/postgres/queue/20-queue.sql` and the pinned digests. Until then a backfill pass is
  claimed ahead of reconciliations and backfills. Separating the lanes in the function is the fix.
- Partitions are created only when a pass needs a month (see "Partitions"); the three months
  ahead scheduler step does not exist.
- Every pass whose inputs moved rewrites the provenance of all the owner's rows (one statement).
  If that becomes slow on long histories, provenance per metric is the change.

Tests: `tests/test_derived_normalize.py` (keys, exactness, flags, skips, order independence,
fingerprint; no database) and `tests/test_derived_normalize_db.py` (rows and provenance,
idempotence, delete and backdate, the purge, ingest and counter interleaved between read and write,
the lock, partitions, the worker context through the runner, the enqueue, the sync hooks).

## Daily values: `metric_daily` (#186)

PLAN §7.1 step 3 and §5. Code: `derived/daily.py` (pure: `compute()`, `fingerprint()`),
`derived/daily_db.py` (read, verify, write) and `derived/daily_job.py` (the `derive_daily` kind and
`enqueue_daily`). #37's `daily_values()` (see [analysis.md](analysis.md)) does the arithmetic.

**Input.** The owner's `observations` of the metric keys of `analysis.quality.METRICS`
(`daily.KEYS`, the keys this pass owns); other keys are never read. A reading is used unless the
normaliser flagged it `unconfirmed`, `implausible` or `duplicate`; `jump` is advisory. The day is
the calendar date in the reading's own `source_tz`, so DST changes, trips and readings around
midnight need no special case. Day assignment by the profile's `fixed_tz` is **not applied** here
(#37's rule is the reading's zone). When it is, the zone setting must become part of the
inputs (the fingerprint), so that a settings change recomputes the affected days; tracked in
#187.

**Row.** `method`, `value`, `morning` (the value, only for `morning`), `min`, `max`, `mad`, `n`
from `DailyValue`; `mean` is the mean of the day's readings in a 60-digit half-even context,
clamped into `[min, max]` (`DailyValue` has none; a mean such as 1/3 has no finite form).
`source_groups` is the `grpid` of the first morning reading, or every `grpid` of the day for a
median, ascending (ADR 0006 §11). A property test checks it against an oracle written from the
rules.

**Provenance.** `algo_version = daily/1`; `input_revision` is the owner's `measures` counter;
`input_fingerprint` is SHA-256 over `daily/1`, `normalize/1` and the sorted observation rows read
(key, group, type, position, instant, zone, value, flags). Every owned row carries the same
fingerprint. A reader compares both values (see "Provenance" above).

**A pass** (`daily_owner`, one owner): (1) read the revision, then the observations. If any
observation this pass reads (its own metric keys, with a source type: the rows derived from
`measures`) carries another revision, the normaliser has not caught up: `behind` (it queues
this pass when it has). Observations of another stream carry that stream's revision and may
have no group id; they are never read, counted or compared. If every stored row
carries this revision, fingerprint and version, `current`. (2) Compute (pure). (3) In one
transaction: take an **owner-scoped advisory lock** (`pg_advisory_xact_lock` on
`metric_daily:<owner>`), lock the revision row `FOR SHARE`, check that the revision and the
observations' fingerprint are the ones read (the fingerprint is **re-read inside the write
transaction**, after the locks; it must stay there), and only then write; otherwise `stale`, nothing
written. A purge of imported data removes the revision row and the observations, so a pass that
read before it writes nothing (the review of #225; tested by interleaving a purge). The write
compares **by content**: only days whose stored values differ are updated, days with no reading
left are deleted, missing days inserted, so a soft delete or a backdated edit changes exactly the
days it touched (`Outcome.affected` lists them), and the provenance of the others moves with one
statement. All statements are scoped to `daily.KEYS`: a `metric_daily` row of any other key is
never deleted or re-stamped. Two concurrent passes for one owner queue on the advisory lock; the
second reads the first's rows after the lock and finds them in place, so neither can hit the
unique key (tested with both passes held between read and write).

Inputs are the observations' content: a purge followed by the same data normalised again is the
same input and a pass that read before it may write (the rows are right). Without the
normalisation in between the observations are gone and the pass is `stale`.

**The job.** `derive_daily`, input `{connection_id, origin: live|backfill}`, registered in the
worker's registry only. `derive_normalize` queues it after a pass that ended `written` or
`current`, with its own origin (a `stale` pass queues nothing: whatever moved its inputs queued a
newer one; nothing imported queues nothing). Separate singleton keys (`daily_live`,
`daily_backfill`); repeats of one origin collapse. It runs as the owner under the fence, so RLS
applies (worker-context test).

**Known gaps.**

- The evaluation job (#187) does not exist yet, so this pass chains to nothing. #187 is queued
  from `run_daily` when it lands.
- The claim lane is the scheduler's (3) for both origins, as for `derive_normalize` (see "Known
  gaps" above).
- A change to one metric moves the shared fingerprint, so one provenance statement re-stamps all
  of the owner's daily rows; the content comparison keeps the rewrite to the days that changed.

Tests: `tests/test_derived_daily.py` (days, DST, trips, midnight, the median and morning sources,
the mean, exclusions, order independence, the property test, the fingerprint; no database) and
`tests/test_derived_daily_db.py` (rows and provenance, idempotence, `behind`, the exactly
affected days after a soft delete and a backdated edit, the purge, ingest, counter and
observation interleavings, the lock, concurrent passes, other metric keys untouched, the worker
context through the runner, the enqueue and the chain from the normaliser).
