import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { getSession, type SessionState } from '../api/auth'
import { setTwoFactorRequiredHandler } from '../api/client'

type Status = 'loading' | 'ready' | 'error'

interface SessionValue {
  status: Status
  session: SessionState | null
  /** Fetches the session again (after sign-in, enrolment, a second-factor code ...). */
  refresh: () => Promise<SessionState | null>
}

const SessionContext = createContext<SessionValue | null>(null)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>('loading')
  const [session, setSession] = useState<SessionState | null>(null)
  // Reads of the session in flight: an API refusal that arrives meanwhile needs no second read.
  const reading = useRef(0)

  const read = useCallback(async () => {
    reading.current += 1
    try {
      return await getSession()
    } finally {
      reading.current -= 1
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const next = await read()
      setSession(next)
      setStatus('ready')
      return next
    } catch {
      setStatus('error')
      return null
    }
  }, [read])

  useEffect(() => {
    let current = true
    read().then(
      (next) => {
        if (!current) return
        setSession(next)
        setStatus('ready')
      },
      () => {
        if (current) setStatus('error')
      },
    )
    return () => {
      current = false
    }
  }, [read])

  // An API call refused with `two_factor_required` means the session's second factor is missing
  // or lapsed: read the session again, and the route guards take the user to the right page.
  useEffect(
    () =>
      setTwoFactorRequiredHandler(() => {
        if (reading.current === 0) void refresh()
      }),
    [refresh],
  )

  const value = useMemo(() => ({ status, session, refresh }), [status, session, refresh])
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext)
  if (value === null) throw new Error('useSession needs a SessionProvider')
  return value
}
