import { useEffect, type ReactNode } from 'react'
import { Card, Notice } from '../components/forms'
import { safeReturnTo, useQuery, useRouter } from '../router/Router'
import { HOME_PATH, accessOf, pathFor, withReturnTo, type Access } from './access'
import { useSession } from './SessionContext'

// Route guards. Each one decides from the session alone and sends the visitor where they
// belong with a replace, so the back button does not return to a page they may not see. The
// server enforces the same rules on every request; these only keep the screens honest.

function Redirect({ to }: { to: string }) {
  const { navigate } = useRouter()
  useEffect(() => {
    navigate(to, { replace: true })
  }, [navigate, to])
  return null
}

/** What to show until the session is known: nothing guarded is shown before that. */
function Waiting() {
  const { status, refresh } = useSession()
  if (status === 'error') {
    return (
      <Card title="Cannot reach the server">
        <Notice kind="error">Try again in a moment.</Notice>
        <button
          className="button"
          type="button"
          onClick={() => {
            void refresh()
          }}
        >
          Try again
        </button>
      </Card>
    )
  }
  return (
    <Card title="One moment">
      <p>Loading</p>
    </Card>
  )
}

/** Where a visitor with `access` goes: onward to `returnTo`, or to the page of their step. */
function destination(access: Access, returnTo: string | null): string {
  return access === 'ready' ? (returnTo ?? HOME_PATH) : withReturnTo(pathFor(access), returnTo)
}

/** The app itself: signed in, with an authenticator app, and through the code prompt. */
export function RequireReady({ children }: { children: ReactNode }) {
  const { status, session } = useSession()
  const { pathname, search } = useRouter()
  if (status !== 'ready') return <Waiting />
  const access = accessOf(session)
  if (access === 'ready') return <>{children}</>
  return <Redirect to={withReturnTo(pathFor(access), pathname + search)} />
}

/** Sign-in and sign-up: a signed-in visitor is sent on, to the step they still owe. */
export function RequireSignedOut({ children }: { children: ReactNode }) {
  const { status, session } = useSession()
  const returnTo = safeReturnTo(useQuery().get('return_to'))
  if (status !== 'ready') return <Waiting />
  const access = accessOf(session)
  if (access === 'anonymous') return <>{children}</>
  return <Redirect to={destination(access, returnTo)} />
}

/**
 * The two pages of the second factor: `enrol` (no authenticator app yet) and `challenge`
 * (a code for this session). Anyone else is sent to their own page. `hold` keeps the page up
 * while it shows something that must not vanish with the redirect, such as recovery codes.
 */
export function RequireTwoFactorStep({
  step,
  hold = false,
  children,
}: {
  step: 'enrol' | 'challenge'
  hold?: boolean
  children: ReactNode
}) {
  const { status, session } = useSession()
  const returnTo = safeReturnTo(useQuery().get('return_to'))
  if (hold) return <>{children}</>
  if (status !== 'ready') return <Waiting />
  const access = accessOf(session)
  if (access === step) return <>{children}</>
  return <Redirect to={destination(access, returnTo)} />
}
