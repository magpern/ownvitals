import type { SessionState } from '../api/auth'

/**
 * How far a visitor is through signing in. Every account needs TOTP (PLAN section 6), so a
 * signed-in session is only `ready` once it has an authenticator app and has passed it.
 *
 * - `anonymous`: not signed in.
 * - `enrol`: signed in, no authenticator app yet: only enrolment is open.
 * - `challenge`: signed in with an authenticator app, but this session has not passed it.
 * - `ready`: signed in and through the second factor.
 */
export type Access = 'anonymous' | 'enrol' | 'challenge' | 'ready'

export const SIGN_IN_PATH = '/signin'
export const ENROL_PATH = '/two-factor/setup'
export const CHALLENGE_PATH = '/two-factor'
export const HOME_PATH = '/'

export function accessOf(session: SessionState | null): Access {
  if (session === null || !session.authenticated) return 'anonymous'
  if (!session.totpEnabled) return 'enrol'
  return session.session?.mfa === true ? 'ready' : 'challenge'
}

/** The page a visitor with this access belongs on, before any `return_to`. */
export function pathFor(access: Access): string {
  switch (access) {
    case 'anonymous':
      return SIGN_IN_PATH
    case 'enrol':
      return ENROL_PATH
    case 'challenge':
      return CHALLENGE_PATH
    case 'ready':
      return HOME_PATH
  }
}

/** `path` with a `return_to` for the page to come back to (none for the home page). */
export function withReturnTo(path: string, target: string | null): string {
  if (target === null || target === HOME_PATH) return path
  return `${path}?return_to=${encodeURIComponent(target)}`
}
