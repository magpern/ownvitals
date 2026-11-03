import { execFileSync } from 'node:child_process'
import { REPO_ROOT } from './env.ts'

/**
 * A fresh single-use invite code from the operator CLI in the running web container (sign-up is
 * by invite). The CLI prints the code once on stdout and its notice on stderr.
 */
export function createInvite(): string {
  const out = execFileSync(
    'docker',
    [
      'compose',
      '-f', 'compose.yaml',
      '-f', 'compose.e2e.yaml',
      'exec', '-T', 'web',
      'python', '-m', 'ownvitals', 'invite', 'create', '--expires', '1d',
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
  )
  const code = out.trim()
  if (!/^\S+$/.test(code)) throw new Error('the invite command printed no single code')
  return code
}

/**
 * Makes the step-up of every session of this account stale, as ten minutes would: the app counts a
 * code within `stepup_window_minutes` (just after enrolment or sign-in it is fresh, so "Sign out
 * everywhere" would go straight through). Done as the database superuser inside the db container,
 * the way the backend tests age a session; the app is not changed.
 */
export function ageStepUp(email: string): void {
  if (!/^[\w.+-]+@[\w.-]+$/.test(email)) throw new Error('unexpected address')
  const sql = `UPDATE sessions SET stepup_at = stepup_at - interval '1 hour' WHERE user_id = (SELECT id FROM users WHERE email_normalized = '${email.toLowerCase()}')`
  const out = execFileSync(
    'docker',
    [
      'compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml',
      'exec', '-T', 'db',
      'psql', '-U', 'postgres', '-d', 'ownvitals', '-v', 'ON_ERROR_STOP=1', '-tA', '-c', sql,
    ],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  )
  if (!/UPDATE [1-9]/.test(out)) throw new Error('no session was aged')
}
