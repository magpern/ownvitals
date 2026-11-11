import { execFileSync } from 'node:child_process'
import { REPO_ROOT } from './env.ts'

// The worker's scheduler ticks every 5 minutes from its own start (`SCHEDULE_TICK_SECONDS`) and
// runs `queue_schedule`: a `withings_poll` and a `withings_reconcile` for every connected
// connection that has had none of that kind within its interval (6 h, 24 h). A connection made a
// minute ago has had neither, so the first tick after a connect polls and reconciles it while its
// first import is still running: the poll queues a second backfill for every stream not yet
// complete and the reconcile re-reads the whole history on a higher lane than the backfill. Where
// that tick falls depends only on when the worker started, so a spec that waits for the import
// to end waits about 2 minutes or about 5 (#403).
//
// `holdScheduler` takes that tick out of a spec that is not about it: it records, for the
// account's live connection, a cancelled poll and a cancelled reconcile created now, which is
// what `queue_schedule` reads ("one in the interval"), so the worker's ticks leave the connection
// alone for the rest of the run. It takes the same advisory lock as `queue_schedule`, so the
// answer is exact: `true` when the hold is in place before any tick, `false` when a tick had
// already scheduled the connection (the hold is then not needed and nothing is written).
// Database superuser, like `ageStepUp`; the e2e stack only.

const KINDS = "('withings_poll', 3), ('withings_reconcile', 4)"

export function holdScheduler(email: string): boolean {
  if (!/^[\w.+-]+@[\w.-]+$/.test(email)) throw new Error('unexpected address')
  const live =
    'SELECT c.id, c.owner_id FROM connections c JOIN users u ON u.id = c.owner_id ' +
    `WHERE u.email_normalized = '${email.toLowerCase()}' AND c.status = 'connected'`
  const sql = `
BEGIN;
SELECT 'live=' || count(*) FROM (${live}) c;
SELECT pg_advisory_xact_lock(1, hashtext(c.owner_id::text))::text FROM (${live}) c;
SELECT 'scheduled=' || count(*) FROM jobs j JOIN (${live}) c
  ON j.owner_id = c.owner_id AND j.input ->> 'connection_id' = c.id::text
  WHERE j.kind IN ('withings_poll', 'withings_reconcile');
INSERT INTO jobs (owner_id, kind, state, input, origin, lane, finished_at)
SELECT c.owner_id, k.kind, 'cancelled', jsonb_build_object('connection_id', c.id), 'scheduler', k.lane, now()
  FROM (${live}) c, (VALUES ${KINDS}) AS k (kind, lane)
  WHERE NOT EXISTS (SELECT FROM jobs j WHERE j.owner_id = c.owner_id AND j.kind = k.kind
                      AND j.input ->> 'connection_id' = c.id::text);
COMMIT;
`
  const out = execFileSync(
    'docker',
    [
      'compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml',
      'exec', '-T', 'db',
      'psql', '-U', 'postgres', '-d', 'ownvitals', '-v', 'ON_ERROR_STOP=1', '-qtA', '-f', '-',
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', input: sql },
  )
  if (!/^live=1$/m.test(out)) throw new Error('the account has no single live connection')
  const scheduled = /^scheduled=(\d+)$/m.exec(out)
  if (scheduled === null) throw new Error('unexpected psql output')
  return scheduled[1] === '0'
}
