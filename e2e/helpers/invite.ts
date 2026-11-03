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
