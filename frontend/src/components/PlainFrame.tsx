import type { ReactNode } from 'react'
import { HOME_PATH } from '../auth/access'
import { useSession } from '../auth/SessionContext'
import { Link } from '../router/Router'
import { LanguagePicker, useT } from '../i18n'
import { Notice } from './forms'
import { useSignOut } from './useSignOut'

/**
 * The frame of every page outside the app: the sign-in, sign-up and mail-link pages, the two
 * second-factor pages and "not found". The product name, then either the way in (signed out) or
 * the account and Sign out (any signed-in session, so a user stuck on the second-factor step can
 * still leave). The signed-in app has the rail and top bar of `AppFrame` instead.
 */
export function PlainFrame({ children }: { children: ReactNode }) {
  const t = useT()
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
            <nav className="nav" aria-label={t('common.account')}>
              <span className="nav__who">{email}</span>
              <button className="button button--secondary" type="button" disabled={busy} onClick={signOut}>
                {t('common.signOut')}
              </button>
            </nav>
          ) : (
            <nav className="nav" aria-label={t('common.account')}>
              <Link to="/signin">{t('common.signIn')}</Link>
              <Link to="/signup">{t('common.signUp')}</Link>
            </nav>
          )
        ) : null}
      </header>
      <main className="shell__main">
        {error ? <Notice kind="error">{error}</Notice> : null}
        {children}
      </main>
      {email === null ? (
        <footer className="shell__footer">
          <LanguagePicker />
        </footer>
      ) : null}
    </div>
  )
}
