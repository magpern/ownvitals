import { useCallback } from 'react'
import { logout } from '../api/auth'
import { SIGN_IN_PATH } from '../auth/access'
import { useSession } from '../auth/SessionContext'
import { useRouter } from '../router/Router'
import { useAction } from './forms'

/**
 * Signing out, for the plain frame's Sign out button and the app frame's account menu: post the
 * logout, read the session again (which empties the account) and go to the sign-in page. A
 * failure leaves the user signed in with `error` to show.
 */
export function useSignOut() {
  const { refresh } = useSession()
  const { navigate } = useRouter()
  const { run, busy, error } = useAction()
  const signOut = useCallback(() => {
    void run(async () => {
      await logout()
      await refresh()
      navigate(SIGN_IN_PATH)
    })
  }, [run, refresh, navigate])
  return { signOut, busy, error }
}
