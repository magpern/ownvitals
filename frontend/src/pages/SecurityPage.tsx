import { useCallback, useEffect, useId, useState } from 'react'
import { listSessions, logoutEverywhere, revokeSession, type SessionItem } from '../api/auth'
import { errorMessage } from '../api/messages'
import { SIGN_IN_PATH } from '../auth/access'
import { useSession } from '../auth/SessionContext'
import { Notice } from '../components/forms'
import { Cancelled, useProtected } from '../components/StepUp'
import { Button } from '../components/ui/Button'
import { Card, ComingLater } from '../components/ui/Card'
import { StatePill } from '../components/ui/DataState'
import { Icon } from '../components/ui/Icon'
import { useRouter } from '../router/Router'

// "Sign-in and security" (/settings/security; docs/design/mockups/src/Security.dc.html). #11
// builds the lock band, "Where you're signed in" (with "Sign out everywhere…" behind the shared
// "Confirm it's you" dialog) and nothing else: the other cards of the mock-up stay "Coming later"
// until their issue (Architect, #11).

/** "Firefox on Windows", from the browser's user agent (shown only to its own user). */
export function deviceName(userAgent: string | null): string {
  if (userAgent === null || userAgent === '') return 'Unknown device'
  const browser = /Edg\//.test(userAgent)
    ? 'Edge'
    : /Firefox\//.test(userAgent)
      ? 'Firefox'
      : /Chrome\//.test(userAgent)
        ? 'Chrome'
        : /Safari\//.test(userAgent)
          ? 'Safari'
          : 'A browser'
  const system = /iPhone/.test(userAgent)
    ? 'iPhone'
    : /iPad/.test(userAgent)
      ? 'iPad'
      : /Android/.test(userAgent)
        ? 'Android'
        : /Windows/.test(userAgent)
          ? 'Windows'
          : /Mac OS X|Macintosh/.test(userAgent)
            ? 'Mac'
            : /Linux/.test(userAgent)
              ? 'Linux'
              : null
  return system === null ? browser : `${browser} on ${system}`
}

const TIME = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' })
const DAY = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' })

/** "Today 07:30" or "27 Oct". */
export function signedInAt(iso: string, now: Date = new Date()): string {
  const at = new Date(iso)
  return at.toDateString() === now.toDateString() ? `Today ${TIME.format(at)}` : DAY.format(at)
}

/** "Now", "5 minutes ago", "2 hours ago", "3 days ago". */
export function lastActive(iso: string, now: Date = new Date()): string {
  const minutes = Math.floor((now.getTime() - new Date(iso).getTime()) / 60000)
  if (minutes < 2) return 'Now'
  if (minutes < 60) return `${String(minutes)} minutes ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return hours === 1 ? '1 hour ago' : `${String(hours)} hours ago`
  const days = Math.floor(hours / 24)
  return days === 1 ? '1 day ago' : `${String(days)} days ago`
}

export function SecurityPage() {
  return (
    <div className="ov-stack" data-gap="lg">
      <p className="ov-band" role="note">
        <Icon name="lock" />
        Actions marked with a lock ask for a fresh code from your authenticator app. One code
        covers 10 minutes of protected actions.
      </p>
      <Card>
        <ComingLater label="How you sign in, the second factor and recent activity">
          Changing the password, Google, email, the authenticator and recovery codes comes in M6.
        </ComingLater>
      </Card>
      <SignedInSessions />
    </div>
  )
}

function SignedInSessions() {
  const headingId = useId()
  const protect = useProtected()
  const { refresh } = useSession()
  const { navigate } = useRouter()
  const [rows, setRows] = useState<SessionItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(
    (signal?: AbortSignal) =>
      listSessions(signal).then(
        (found) => {
          setRows(found)
          setError(null)
        },
        (caught: unknown) => {
          if (!signal?.aborted) setError(errorMessage(caught))
        },
      ),
    [],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => {
      controller.abort()
    }
  }, [load])

  const run = async (task: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await task()
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  const signOutOne = (id: string) => {
    void run(async () => {
      await revokeSession(id)
      await load()
    })
  }

  const signOutEverywhere = () => {
    void run(async () => {
      await protect('Signing out everywhere', logoutEverywhere)
      await refresh()
      navigate(SIGN_IN_PATH)
    })
  }

  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <div className="ov-between">
        <h2 id={headingId} className="ov-card__title">
          Where you&apos;re signed in
        </h2>
        <Button variant="danger" lock disabled={busy} onClick={signOutEverywhere}>
          Sign out everywhere…
        </Button>
      </div>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {rows === null ? (
        error === null ? <p className="ov-muted">Loading</p> : null
      ) : (
        <div className="ov-tbl-wrap">
          <table className="ov-tbl">
            <thead>
              <tr>
                <th scope="col">Device</th>
                <th scope="col">Place and IP</th>
                <th scope="col">Signed in</th>
                <th scope="col">Last active</th>
                <th scope="col">
                  <span className="ov-sr">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={row.current ? 'ov-tbl__sel' : undefined}>
                  <td>
                    <strong>{deviceName(row.userAgent)}</strong>
                    {row.current ? <StatePill tone="ok">This device</StatePill> : null}
                  </td>
                  <td>{row.ip !== null ? <span className="ov-mono">{row.ip}</span> : 'Unknown'}</td>
                  <td className="ov-num">{signedInAt(row.createdAt)}</td>
                  <td>{row.current ? 'Now' : lastActive(row.lastSeenAt)}</td>
                  <td>
                    {row.current ? null : (
                      <Button
                        disabled={busy}
                        aria-label={`Sign out ${deviceName(row.userAgent)}`}
                        onClick={() => {
                          signOutOne(row.id)
                        }}
                      >
                        Sign out
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="ov-small ov-muted">
        A session ends after 72 hours without activity, and after 30 days at most.
      </p>
    </section>
  )
}
