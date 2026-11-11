import { useEffect, type ReactNode } from 'react'
import { decodeStatus } from '../../api/overview'
import { useResource } from '../../api/useResource'
import { HOME_PATH, isAdmin } from '../../auth/access'
import { useSession } from '../../auth/SessionContext'
import { useT } from '../../i18n'
import { Link } from '../../router/Router'
import type { NavId } from '../../router/appRoutes'
import { Notice } from '../forms'
import { railStatus } from '../overview/overviewModel'
import { useSignOut } from '../useSignOut'
import { Logo } from '../ui/Icon'
import { AccountMenu } from './AccountMenu'
import { FreshnessLine } from './FreshnessLine'
import { PhoneTabs, Rail, type NavStatuses } from './Rail'
import { phoneTabOf } from './nav'
import { StatusContext } from './statusContext'
import { DisplayProvider } from '../../display/DisplayContext'

// The frame of every app page (docs/design/mockups/src/Dashboard.dc.html and the others): the
// navigation rail on the left, and on the right a top bar (the page header, the account menu)
// above the page. On a phone the rail becomes four tabs at the bottom and the top bar carries
// the logo.

interface AppFrameProps {
  /** The page header: the h1 of the top bar. */
  title: string
  /** The navigation item that is current. */
  active: NavId | null
  /** The address of the page (without a trailing slash). */
  path: string
  /** What the rail shows beside its items (counts, dots); none until the screens that own them. */
  status?: NavStatuses
  children: ReactNode
}

export function AppFrame({ title, active, path, status, children }: AppFrameProps) {
  const { session } = useSession()
  const { signOut, error } = useSignOut()
  const email = session?.authenticated === true ? (session.user?.email ?? '') : ''
  const admin = isAdmin(session)
  const t = useT()
  // One read of the status for the freshness line, the rail's marks and the stream tiles.
  const statusResource = useResource('/api/status', decodeStatus)
  const marks = { ...(statusResource.data === null ? {} : railStatus(statusResource.data, t)), ...status }

  // A change of page changes the document title too (it is what a screen reader announces).
  useEffect(() => {
    document.title = `${title} \u00b7 ownvitals`
    return () => {
      document.title = 'ownvitals'
    }
  }, [title])

  return (
    <DisplayProvider>
      <StatusContext.Provider value={statusResource}>
        <div className="ov-app">
          <div className="ov-rail-wrap">
            <Rail active={active} path={path} admin={admin} status={marks} />
          </div>
          <div className="ov-main">
            <header className="ov-topbar">
              <Link className="ov-topbar__logo" to={HOME_PATH} aria-label="ownvitals">
                <Logo />
              </Link>
              <h1 className="ov-topbar__title">{title}</h1>
              <AccountMenu email={email} admin={admin} onSignOut={signOut} />
            </header>
            <FreshnessLine status={statusResource.data} />
            <main className="ov-content">
              {error ? <Notice kind="error">{error}</Notice> : null}
              {children}
            </main>
          </div>
          <PhoneTabs active={active === null ? null : phoneTabOf(active)} path={path} />
        </div>
      </StatusContext.Provider>
    </DisplayProvider>
  )
}
