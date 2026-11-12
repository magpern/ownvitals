# Derived data: schema

The tables the analysis writes (PLAN §5 and §7, roadmap Amendments 2 and 3, ADR 0010 §4,
ADR 0002 §2). Migration `0011_analysis_schema` creates them; `ownvitals.derived.tables` holds the
Core tables and the vocabularies the migration's check constraints accept. Writers come with the
M4 wiring issues; this file starts with the schema (#181) and goes on with the writers:
normalisation into `observations` (#185, and the sleep and activity streams of #229), the daily
values in `metric_daily` (#186) and the rule evaluation into `findings` (#187).

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

- `rule_id` is a capital, one or two digits, then optionally `x` or `:<part>` (migration 0016,
  #187: the ids of `analysis.rules.core.SPECS`, `C1x`, `N2:guided`, `Q1:no_weigh_in` ...);
  `tier = 'system'` exactly when the id is `Q1` or `Q1:<part>` (Amendment 3); device and system
  findings are `confidence = 'high'` (ADR 0010 §5).
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
`normalise()`, `fingerprint()`), `derived/normalize_streams.py` (pure: the sleep and activity
streams, #229), `derived/normalize_db.py` (the sources; read, verify, write) and
`derived/normalize_job.py` (the `derive_normalize` kind and `enqueue_normalize`).

"Pure" means no database, clock or randomness. `metric_keys.py` imports `derived.tables` for
`METRIC_KEY_PATTERN`, so importing the pure modules loads SQLAlchemy's Core table definitions
(no engine or session); accepted (#229), and moving the pattern to a module with no imports
would make the purity literal.

**Keys.** `metric_key(meastype, position)`: the keys of `analysis.quality.METRICS` for the types
the analysis judges (`height` among them, 0.5 to 2.6 m, an engineering bound); the three temperatures, `vo2_max`, the two AFib enums and
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

**Sources.** A pass covers one *source*, one input stream with its own `data_revisions` counter
(`normalize_db.SOURCES`): `measures` (above), `sleep_summary`, `activity_daily` and `workouts`
(below). The job runs all four, one pass each (`normalise_all`).

**A pass** (`normalise_owner`, one owner, one source, all connections): (1) read the revision,
then the source rows (for measures, the groups and measures); if every stored row already carries this revision, fingerprint and algorithm
version, stop (`current`); (2) compute, and create the missing months with `ensure_month`, one
committed transaction per month; (3) in one transaction **take the owner's normalisation lock, lock the
revision row `FOR SHARE`, check that it and the source rows' fingerprint are the ones read, and
only then write** by natural key
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
- Every pass whose inputs moved rewrites the provenance of all the rows its source owns (one
  statement). If that becomes slow on long histories, provenance per metric is the change.
- `sleep_series` and `intraday` are not normalised: no rule (ADR 0010) or score (ADR 0012) reads
  them, and their volume (minute samples, tens of thousands of rows a night) would make every
  pass a long write holding the revision lock. They stay in their own tables; a later issue adds
  them as sources (incrementally, by month) if an analysis needs them.
- The stream fields' units are `unknown` (`e<unit>:unknown`): the findings document none. The
  field names ADR 0012 reads are documented assumptions (the probe settles them); `rmssd` in a
  sleep summary is assumed outright, because the public summary shape lists no HRV field.

**Sleep and activity streams (#229).** `derived/normalize_streams.py`:

| Source (`data_revisions` stream) | Table | Keys | `ts` | `source_ts` | `source_grpid` |
|---|---|---|---|---|---|
| `sleep_summary` | `sleep_summaries` | `sleep_<field>` of `raw.data` | the night's end (wake-up) | its start | the summary's Withings id |
| `activity_daily` | `activity_daily` | `activity_<field>` of `raw.fields` | 12:00 local on its date in its zone | the stored `start_at` | none |
| `workouts` | `workouts` | `workout_<field>` of `raw.data` | its start | its start | the workout id |

The known fields are `SLEEP_FIELDS` (`withings.models.SleepSummaryData`'s numbers, plus `rmssd`),
`ACTIVITY_FIELDS` (the ingest's, `ingest.codec.ACTIVITY_FIELDS`) and `WORKOUT_FIELDS`
(`WorkoutData`'s); tests pin each table to its model. A summary's or a workout's other numeric
fields are kept as well (`sleep_<name>`, `workout_<name>`); an activity day's stored `fields`
hold the known ones only. `source_type` and `source_position` are NULL. A value is the stored
decimal text read exactly (`value_raw x 10^unit`, `conversion` `e<unit>:unknown`); there are no
quality flags (no `attrib`). A known field that is not a number, or an extra whose name cannot be
a key, counts as `invalid`; the other skips are the measures' ones. Two connections reporting
the same night or day keep one row (the first in canonical order). An activity day's `ts` is
local noon so a day assignment within ±11 h of its zone keeps its date; a sleep summary's local
day of `ts` is ADR 0012 §4's "night whose wake-up falls on *d*".

What ADR 0012 reads (`SCORE_INPUTS`; ADR 0010's v1 rules read no stream field, its §8): night
heart rate `sleep_hr_average`, AHI `sleep_apnea_hypopnea_index`, Readiness's duration
`sleep_total_sleep_time` and HRV `sleep_rmssd`, daily steps `activity_steps`, and the active
minutes `activity_moderate` and `activity_intense`.

Provenance: `algo_version = normalize-streams/1`; `input_revision` is the source's own
`data_revisions` counter; `input_fingerprint` is SHA-256 over the version, the stream name and the
sorted `(row id, payload hash)` of the owner's rows of that table (`stream_fingerprint()`).

**Ownership (#229).** The passes share `observations`, so each owns a disjoint part of it and
every statement of its diff (state, months, existing, update, delete, provenance) is limited to
that part: measures own the rows with a `source_type`; each stream owns the rows with none whose
key starts with its prefix (`sleep_`, `activity_`, `workout_`; `starts_with`, since `_` is a LIKE
wildcard). A computed row outside the pass's part stops the pass (`RuntimeError`) before
anything is written. A row no pass produces (another writer's) is never read, changed, deleted
or re-stamped by a pass. A future writer of `observations` takes a part disjoint from these.

**Concurrent passes (#229).** A live and a backfill pass of one owner can run together (separate
singleton keys, one lane). The write transaction first takes `pg_advisory_xact_lock(6,
hashtext(owner))` (namespace 6, normalisation, ADR 0005 §9), then the revision row: the second
pass waits, then diffs against what the first wrote and finds nothing to insert, so both end
without error. Nothing else takes namespace 6, so the order (advisory, then the revision row)
makes no cycle with ingest or the purge. The wait counts against the role's 5 s `lock_timeout`; a
write longer than that (a first pass over a long history) fails the waiting pass's attempt, which
is retried.

**Enqueue.** A backfill or sync of a stream in `normalize_job.TRIGGERS` (`measures`,
`sleep_summary`, `activity_daily`, `workouts`) queues a pass. The enqueue is best-effort: the
ingest has committed, so a database error (the connection was disconnected while the job ran, a
lost connection) is logged by its class name and the job completes; the next sync or the
nightly reconciliation queues the pass again. Any other exception is raised.

Tests: `tests/test_derived_normalize.py` (keys, exactness, flags, skips, order independence,
fingerprint; no database), `tests/test_derived_normalize_streams.py` (the stream key tables
pinned to the models, ADR 0012's inputs, exactness, instants, skips, duplicates, order; no
database), `tests/test_derived_normalize_streams_db.py` (each stream's rows and provenance, a
second writer's rows surviving every pass, two concurrent passes, the owner's lock, the purge,
counter, fingerprint and lock tests for each stream, the job over every source, the triggers, the
best-effort enqueue) and `tests/test_derived_normalize_db.py` (rows and provenance,
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
transaction: take the owner's **normalisation lock** (`pg_advisory_xact_lock(6, hashtext(owner))`,
namespace 6, ADR 0005 §9: the lock the normaliser's write takes, so the two never write together),
lock the revision row `FOR SHARE`, check that the revision and the
observations' fingerprint are the ones read (the fingerprint is **re-read inside the write
transaction**, after the locks; it must stay there), and only then write; otherwise `stale`, nothing
written. A purge of imported data removes the revision row and the observations, so a pass that
read before it writes nothing (the review of #225; tested by interleaving a purge). The write
compares **by content**: only days whose stored values differ are updated, days with no reading
left are deleted, missing days inserted, so a soft delete or a backdated edit changes exactly the
days it touched (`Outcome.affected` lists them), and the provenance of the others moves with one
statement. All statements are scoped to `daily.KEYS`: a `metric_daily` row of any other key is
never deleted or re-stamped. Two concurrent passes for one owner queue on that lock; the
second reads the first's rows after the lock and finds them in place, so neither can hit the
unique key (tested with both passes held between read and write).

Inputs are the observations' content: a purge followed by the same data normalised again is the
same input and a pass that read before it may write (the rows are right). Without the
normalisation in between the observations are gone and the pass is `stale`.

**Day assignment (#187).** The profile's day setting (`user_profiles.day_assignment`, `day_tz`;
ADR 0010 §1) is applied here: under `fixed_tz` every reading is given the fixed zone, for its day
and for the morning window. No profile row is the default, each reading's own zone. The setting is
part of the fingerprint (`daily.day_setting`), so changing it makes every row stale and the next
pass moves exactly the days it changes; it is re-read inside the write transaction with the
observations (a change between read and write is `stale`). Nothing writes the setting yet: the
Settings writer (M6) must queue a daily pass when it changes it, or the change waits for the next
sync or the nightly reconciliation.

**The job.** `derive_daily`, input `{connection_id, origin: live|backfill}`, registered in the
worker's registry only. `derive_normalize` queues it after a pass that ended `written` or
`current`, with its own origin (a `stale` pass queues nothing: whatever moved its inputs queued a
newer one; nothing imported queues nothing). It queues the rule evaluation (`derive_evaluate`) the
same way. Separate singleton keys (`daily_live`,
`daily_backfill`); repeats of one origin collapse. It runs as the owner under the fence, so RLS
applies (worker-context test).

**Known gaps.**

- A pass after an import that has not been normalised at all (no observation yet) is `current`
  with nothing to do, not `behind`: an empty table carries no revision to compare.
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

## Rule evaluation: `findings` (#187)

PLAN §7.1 steps 6 and 7, ADR 0010 §1 and §4. Code: `derived/evaluate.py` (pure: `evaluate()`,
`fingerprint()`), `derived/findings_store.py` (read, verify, write), `derived/evaluate_job.py`
(the `derive_evaluate` kind and `enqueue_evaluate`) and `derived/check_cli.py` (`ownvitals
analysis check`). The rules themselves are `analysis.rules` ([analysis-rules.md](analysis-rules.md)).

**Rules.** Every rule of `SPECS`: B1 to B6, C1, C1x, C2, N2 (`N2:guided`, `N2:routine`), N3, N5
and Q1's four parts, and since #191 the ECG rules E1 to E4 and E6 ([ecg.md](ecg.md)); every
statement is limited to `evaluate.OWNED_RULES`, so another rule id (E5 is deferred) is never read,
changed or deleted here. **Experimental rules** (B1, B3, C1x, E4) are evaluated only when the owner-only switch is effective (`flags.effective.
experimental_findings`: the environment and the stored flag; until M8 every account is a household
account, so the switch is the whole rule). Without it they are not computed and nothing of theirs
is written. **Switch turned off later:** the next pass deletes the stored experimental rows
(a stored row no recompute produces is deleted, below); until then the read function already hides
them (`views.findings_read` drops a row by its experimental rule id whatever its tier). Turned on
again, they are recomputed as new episodes: origin from that first write, so an old one is
`backfill` and a current one `live` (and notifies once more).

**Inputs** (`findings_store.gather`, one transaction): the `measures` revision, the day setting,
the observations of `daily.KEYS` (the daily pass's), the stored `metric_daily` rows, the segmental
muscle (175) and per-foot (158, 159, 197, 198) observations, the `measure_groups.attrib` of the
nerve readings (guided is 15), Q1's state (live connections through `connections.state`, the one
read of that table's ids and statuses outside the repository's own sessions, so the crypto guard
keeps the table in `ownvitals.connections`; their `sync_cursors` capability and
`backfill_state.status`, `devices.battery`, queued or running `withings_backfill` and
`withings_connect` jobs) and the switch. A reading is used unless flagged `unconfirmed`,
`implausible` or `duplicate`; under `fixed_tz` it gets the fixed zone. `jump` is criterion J's
flag, carried on the daily value's source groups. **Today** is `now` in the fixed zone, or in the
zone of the most recent usable reading (UTC with none). Activity, sleep and workout observations
are not read: no v1 rule uses them; a later rule reads the activity day as stored (#230).
`nothing` without a revision row; `behind` when an observation read carries another revision or
the stored daily rows are not those of these observations and this day setting (provenance and
count compared with a recompute), so the evaluation never reads stale daily values.

**Replay.** Each (rule, subject) is evaluated on every evaluation day in order (its trigger's
days: a daily value, a guided or routine reading, a group with a pair) and folded through
`advance_simple` or, for the trend-test rules, `advance_persistent`. A rule at day *d* reads only
data up to *d*, so the whole-history replay gives the episodes an incremental evaluation in data
order would have given, and a recompute after a data revision re-derives them. C1 and N2 routine
memoise their band per first day (`bands=`; the band at *q* reads only days before *q*), so a
replay is linear in bands. Q1 has no history: each part is evaluated on today and applied to the
stored active episode (`advance_simple`): fire and none active, open; fire while active, update
(reference day and origin stay); no fire, or a device or stream no longer reported, end. Ended Q1
episodes are history and never touched.

**Episodes and the diff** (ADR 0010 §4). A stored and a computed episode with the same rule,
subject and reference day are one episode: it keeps its id, `first_seen`, `origin`,
`notify_state`, `notified_at` and `seen_at`; the rest is updated when it differs. A computed
episode with no match is inserted with `origin = origin(window_end, today)`, computed at this first
write from the window end it has then and never changed, and `notify_state = 'pending'` exactly
when that origin is `live` ("to notify": sending is the notifications issue). Backfilled history is
`backfill` and `none`. A stored episode no recompute produces is deleted (its data changed or was
removed, or the switch is off). Statements run deletes, ends, updates, inserts, so the one active
episode per (owner, rule, subject) index holds throughout. `first_seen`, `last_seen` and `ended_at`
are the evaluation days of the first firing, the latest firing and the end, stored at 00:00 UTC, so
a recompute of the same data is byte-identical and a second run changes nothing (`current`).

**Row.** `rule_version` is `rules/1.<rule version>` (the shared bands and confidence, then the
rule's own `SPECS` version); `tier`, `severity`, `confidence`, `evidence` (with `trigger_groups`)
and `data_quality` are the latest firing's, a non-finite number stored as `null`; `metric_keys`
are the rule's inputs (B6: the two segment keys; N5: `meastype_158`/`meastype_159`; Q1: `weight`
for no weigh-in, none otherwise). Subjects follow ADR 0010 §4; a device id the subject check
cannot hold is skipped.

**Provenance.** `input_revision` is the `measures` counter: the one stream the v1 rules read
(through observations and daily values); Q1's state records are not a `data_revisions` stream.
`input_fingerprint` is `evaluate.fingerprint`: SHA-256 over the rule versions, today, the switch,
the daily values with their groups and jump flags, the readings and segments, and Q1's state.
Every re-derived row carries both; an ended Q1 episode keeps the provenance it ended with.

**Write** (`findings_store._write`): the writer discipline of `daily_db`. One transaction takes the
owner's normalisation lock (`pg_advisory_xact_lock(6, hashtext(owner))`), locks the revision row
`FOR SHARE`, **gathers every input again** and compares the revision and the fingerprint with the
ones read; otherwise `stale` and nothing is written. A purge of imported data between read and write
removes the revision row, so the pass writes nothing (tested by interleaving the purge). Two passes
of one owner queue on the lock; the second finds the first's rows and ends `current`.

**The job.** `derive_evaluate`, input `{connection_id, origin}`, separate singleton keys
(`evaluate_live`, `evaluate_backfill`), registered in the worker's registry only; queued by
`derive_daily` after a `written` or `current` pass, so the nightly reconciliation re-evaluates
Q1's time-dependent part every day. It runs as the owner under the fence (RLS; worker-context
test). Settings come from the environment (`Settings()`), the switch from the database.

**`ownvitals analysis check --user <uuid>`** (M4 "reproducible on your data"). One read-only user
session: recomputes the daily values from the observations and compares them with `metric_daily`
by content, then gathers the evaluation inputs, evaluates and counts the diff the worker would
apply. It prints counts, versions, rule ids and states only (never a value, date, timestamp,
subject or id; a probe-style shape test), exits 0 on a match and 1 on a mismatch. Run it after the
day's evaluation: `today` and Q1's 14 days move with time.

**Known gaps.**

- Every pass replays the whole history (about 3 s of CPU for two years of seven daily metrics, measured
  locally). Incremental evaluation from the earliest changed day needs the persistence state
  stored; not needed at household scale.
- A connection-status or device-battery change is evaluated at the next pass (a sync or the nightly
  chain); nothing queues a pass on the change itself.
- Without a `measures` revision row (nothing imported) nothing is evaluated, Q1 included.
- The UI copy of the "delete imported data" option must say "all imported data" (it removes every
  connection's imported rows and all derived rows, `user_profiles` stays); the connections UI that
  shows it is not on `main` yet (M6).

Tests: `tests/test_derived_evaluate.py` (the rule tables, replay, persistence, the band memo, the
switch, the reading-level rules, Q1, the fingerprint, the diff; no database),
`tests/test_derived_evaluate_db.py` (a synthetic year's episodes, a second run, origin and
notification, re-derivation, the switch on, off and stored off, Q1, the purge, ingest, daily and
switch interleavings, the lock, concurrent passes, other writers' rules, the worker context, the
enqueue and the chain from the daily pass), `tests/test_derived_evaluate_schema.py` (migration 0016
and the read function's `C1x` drop) and `tests/test_derived_check.py` (the check command).

## Scores: `scores` (#188)

ADR 0012. Pure code: `analysis.scores` (`body_path`, `readiness`, `his`, `his_table`,
`his_inputs`; no I/O). Database side: `derived/scores_gather.py` (read), `scores_compute.py`
(pure glue), `scores_store.py` (verify and write), `scores_tables.py` (the optional HIS band
table), `scores_job.py` (the `derive_scores` kind, worker registry only). The read API is
`views/scores_routes.py` over `views.reads.latest_scores` (the allowlisted session module).
Setting: `config_scores.ScoreSettings` (`OWNVITALS_HIS_BAND_TABLE`; defaults to the shipped `his_tables/withings_card_v1.json`, empty
unsets it).

**Chain.** `derive_daily` -> `derive_evaluate` -> `derive_scores`: `derive_evaluate` queues the
scores job after a pass that ended `written` or `current`, so the nightly reconciliation also
recomputes the day's scores.

**Rows.** One per `(owner_id, kind, day)`: `body_path:<key>`, `readiness`, and `his` only while the
owner-only switch (`flags.effective.experimental_findings`) is effective; with it off the pass
deletes the stored `his` rows. `value` is set only when `components.status` is `computed`; every
other status (`not_set`, `not_available`, ...) leaves it null and names its reason. HIS uses the
shipped Withings card table (ADR 0012 owner question 1, 2026-11-12): only the cells findings §3
documents, so a user outside a documented age or sex bracket gets that criterion `not_available`
(`no_band_row`). Still experimental (owner-only flag).
`input_revision` is `scores_revision.revision_digest` of the three stream counters read (63 bits of a
SHA-256 over the ordered counters; the old sum was ambiguous, `(2,0,0)` and `(1,1,0)` summed equal).
No migration: a row stored under the old sum reads as stale and the next pass rewrites it.

**Purge race.** The pattern of `daily_db`: write transaction takes the NORMALISATION lock, locks
the `data_revisions` rows `FOR SHARE`, gathers the inputs again and writes only when the
revisions and the fingerprint are the ones read (else `stale`). An upsert that changes nothing
reports `current`.

**Not wired (reported per row, never guessed).** BMI reads the newest confirmed height
observation (meastype 4, metres) through the owner's session; none: `no_height`. Quality `quality/2`
flags a height outside 0.5 to 2.6 m `implausible`, so the read skips it. **Assumption:** the newest
height ever is used for every day, so a later height changes the BMI of earlier days. The height
(timestamp and value) is part of the fingerprint. `components.vertical_reasons` carries one code
per area without a score (`no_height`, `insufficient_data`, `no_band_row`, `not_supported`,
`not_available`, `birth_year_unset`, `sex_unset`; `analysis/scores/his_reasons.py`), which the
card words in en and sv. `afib_ecg_count` and `high_bp_ratio` inputs are `input_not_wired`. Sleep and
activity field names and units are assumptions until the owner's probe confirms them.

**Tests.** `tests/analysis/scores/` (pure), `tests/test_derived_scores_hardening_db.py` (#302: concurrent writers, fingerprint cases, zone
change, digest, stale on read), `tests/test_derived_scores_db.py` (database: idempotence,
one row per kind and day, the switch, the purge and ingest races, isolation).

**Scores: trigger, day, stale (#188 fix round).** An owner with only sleep or activity data has no
measures pass, so `derive_normalize` queues `derive_scores` itself when the `sleep_summary` or
`activity_daily` pass wrote or was current and `measures` did not. The pass's "today" follows
ADR 0010 §1 (`findings_gather.today_in_zone`: the fixed zone, else the zone of the latest usable
reading, else UTC). `GET /api/scores` marks a row `stale` when the digest of the stream revisions differs
from its `input_revision` (the fingerprint is not recomputed on a read). **Decision (#302):** a
read covers the stream revisions only; a change of profile, fixed zone, owner-only switch or band
table shows on the next pass, which the nightly reconciliation runs (pinned by
`test_a_read_covers_the_stream_revisions_only`). Assumption: the last
sleep summary of a wake-up day wins, so a nap on the same day replaces the main night.
