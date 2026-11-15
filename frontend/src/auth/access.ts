import type { SessionState } from '../api/auth'

/**
 * How far a visitor is through signing in. The authenticator app is optional for users and
 * required for administrators (owner decision of 2026-11-12, issue 475); the server says which in the
 * session answer (`mfa`: the session owes nothing more).
 *
 * - `anonymous`: not signed in.
 * - `enrol`: signed in to an account that must have an authenticator app and has none yet: only
 *   enrolment is open.
 * - `challenge`: signed in with an authenticator app, but this session has not passed it.
 * - `ready`: signed in and owing nothing: through the code, or without an authenticator app.
 */
export type Access = 'anonymous' | 'enrol' | 'challenge' | 'ready'

export const SIGN_IN_PATH = '/signin'
export const ENROL_PATH = '/two-factor/setup'
export const CHALLENGE_PATH = '/two-factor'
// The signed-in start page. `/` belongs to the public home page (ADR 0016 section 2): the server
// answers it with the prerendered page, and the app's own `/` only redirects here.
export const HOME_PATH = '/overview'

export function accessOf(session: SessionState | null): Access {
  if (session === null || !session.authenticated) return 'anonymous'
  if (session.session?.mfa === true) return 'ready'
  return session.totpEnabled ? 'challenge' : 'enrol'
}

/**
 * A ready session of an account that may set up an authenticator app but has not (issue 475): the
 * enrolment page is open to it with "Skip for now", and it is offered after each sign-in.
 */
export function mayAddTotp(session: SessionState | null): boolean {
  return accessOf(session) === 'ready' && session?.totpEnabled === false && !session.totpRequired
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

/**
 * Whether this session belongs to an administrator: the `admin` role (ADR 0002). It decides what
 * to show (the Admin item of the rail and the account menu, the admin screens); the server decides
 * what an admin may do. It does not look at the second factor: the guards already require
 * `ready` before any app screen.
 */
export function isAdmin(session: SessionState | null): boolean {
  return session?.authenticated === true && session.user?.role === 'admin'
}
