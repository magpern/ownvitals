import type { ReactNode } from 'react'
import { HOME_PATH } from '../auth/access'
import { useSession } from '../auth/SessionContext'
import { Link } from '../router/Router'
import { Notice } from './forms'
import { useSignOut } from './useSignOut'

/**
 * The frame of every page outside the app: the sign-in, sign-up and mail-link pages, the two
 * second-factor pages and "not found". The product name, then either the way in (signed out) or
 * the account and Sign out (any signed-in session, so a user stuck on the second-factor step can
 * still leave). The signed-in app has the rail and top bar of `AppFrame` instead.
 */
export function PlainFrame({ children }: { children: ReactNode }) {
  const { status, session } = useSession()
  const { signOut, busy, error } = useSignOut()
  const email = session?.authenticated === true ? (session.user?.email ?? null) : null

  return (
    <div className="shell">
      <header className="shell__header">
        <Link className="brand" to={HOME_PATH}>
          ownvitals
        </Link>
        {status === 'ready' ? (
          email !== null ? (
            <nav className="nav" aria-label="Account">
              <span className="nav__who">{email}</span>
              <button className="button button--secondary" type="button" disabled={busy} onClick={signOut}>
                Sign out
              </button>
            </nav>
          ) : (
            <nav className="nav" aria-label="Account">
              <Link to="/signin">Sign in</Link>
              <Link to="/signup">Sign up</Link>
            </nav>
          )
        ) : null}
      </header>
      <main className="shell__main">
        {error ? <Notice kind="error">{error}</Notice> : null}
        {children}
      </main>
    </div>
  )
}
