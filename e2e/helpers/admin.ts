import { execFileSync } from 'node:child_process'
import { REPO_ROOT } from './env.ts'

/**
 * Makes an existing synthetic account an admin with the operator CLI in the running web container
 * (`ownvitals admin grant`), as the operator would. The grant signs the account out everywhere;
 * the role takes effect in a session that passed the authenticator code (ADR 0003 section 2).
 */
export function grantAdmin(email: string): void {
  if (!/^[\w.+-]+@[\w.-]+$/.test(email)) throw new Error('unexpected address')
  execFileSync(
    'docker',
    [
      'compose', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml',
      'exec', '-T', 'web',
      'python', '-m', 'ownvitals', 'admin', 'grant', email,
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'ignore', 'pipe'] },
  )
}
