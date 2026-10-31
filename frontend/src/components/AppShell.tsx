import type { ReactNode } from 'react'
import { logout } from '../api/auth'
import { HOME_PATH, SIGN_IN_PATH } from '../auth/access'
import { useSession } from '../auth/SessionContext'
import { Link, useRouter } from '../router/Router'
import { Notice, useAction } from './forms'

/**
 * The page frame: the product name, then either the way in (signed out) or the account and
 * Sign out (any signed-in session, so a user stuck on the second-factor step can still leave).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const { status, session, refresh } = useSession()
  const { navigate } = useRouter()
  const action = useAction()
  const email = session?.authenticated === true ? (session.user?.email ?? null) : null

  const signOut = () => {
    void action.run(async () => {
      await logout()
      await refresh()
      navigate(SIGN_IN_PATH)
    })
  }

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
              <button
                className="button button--secondary"
                type="button"
                disabled={action.busy}
                onClick={signOut}
              >
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
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        {children}
      </main>
    </div>
  )
}
