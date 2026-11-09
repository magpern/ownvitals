import { execFileSync } from 'node:child_process'
import { REPO_ROOT } from './env.ts'

/**
 * Empties the stack's rate-limit buckets, as a new hour would. The specs share one client IP,
 * and the sign-up limit (5 per hour per IP, ADR 0003 section 9) is spent by the specs that run
 * before; a spec that signs up more accounts starts from a clean budget. Done as the database
 * superuser inside the db container, like `ageStepUp`; the app and its limits are not changed.
 * Only the e2e stack's own database is touched.
 */
export function resetRateLimits(): void {
  const out = execFileSync(
    'docker',
    [
      'compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml',
      'exec', '-T', 'db',
      'psql', '-U', 'postgres', '-d', 'ownvitals', '-v', 'ON_ERROR_STOP=1', '-tA', '-c', 'DELETE FROM rate_limits',
    ],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  )
  if (!/DELETE \d+/.test(out)) throw new Error('the rate limits were not reset')
}
