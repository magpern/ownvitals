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
import { getLocale, plural, t as translate, useT } from '../i18n'
import type { Locale } from '../i18n/locale'
import { browserZone, formatDateOfInstant, formatTime } from '../lib/format'
import { useRouter } from '../router/Router'
import { SettingsTabs } from './settings/SettingsTabs'

// "Sign-in and security" (/settings/security; docs/design/mockups/src/Security.dc.html). #11
// builds the lock band, "Where you're signed in" (with "Sign out everywhere…" behind the shared
// "Confirm it's you" dialog) and nothing else: the other cards of the mock-up stay "Coming later"
// until their issue (Architect, #11). The account's language (ADR 0015 section 2, issue 249) is a
// row of Settings → Profile and display (issue 203).

/** "Firefox on Windows", from the browser's user agent (shown only to its own user). */
export function deviceName(userAgent: string | null): string {
  if (userAgent === null || userAgent === '') return translate('security.device.unknown')
  const browser = /Edg\//.test(userAgent)
    ? 'Edge'
    : /Firefox\//.test(userAgent)
      ? 'Firefox'
      : /Chrome\//.test(userAgent)
        ? 'Chrome'
        : /Safari\//.test(userAgent)
          ? 'Safari'
          : translate('security.device.aBrowser')
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
  return system === null ? browser : translate('security.device.on', { browser, system })
}

/** "Today 07:30" or "27 Oct", in the browser's zone: a session belongs to no reading. */
export function signedInAt(iso: string, now: Date = new Date(), locale: Locale = getLocale()): string {
  const at = new Date(iso)
  const zone = browserZone()
  if (at.toDateString() !== now.toDateString()) return formatDateOfInstant(locale, at, zone)
  return translate('security.time.today', { time: formatTime(locale, at, zone) })
}

/** "Now", "5 minutes ago", "2 hours ago", "3 days ago". */
export function lastActive(iso: string, now: Date = new Date()): string {
  const minutes = Math.floor((now.getTime() - new Date(iso).getTime()) / 60000)
  if (minutes < 2) return translate('common.now')
  if (minutes < 60) return plural('security.ago.minutes', minutes)
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return plural('security.ago.hours', hours)
  return plural('security.ago.days', Math.floor(hours / 24))
}

export function SecurityPage() {
  const t = useT()
  return (
    <div className="ov-stack" data-gap="lg">
      <SettingsTabs />
      <p className="ov-band" role="note">
        <Icon name="lock" />
        {t('security.band')}
      </p>
      <Card>
        <ComingLater label={t('security.laterLabel')}>{t('security.later')}</ComingLater>
      </Card>
      <SignedInSessions />
    </div>
  )
}

function SignedInSessions() {
  const t = useT()
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
      await protect(t('security.sessions.purpose'), logoutEverywhere)
      await refresh()
      navigate(SIGN_IN_PATH)
    })
  }

  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <div className="ov-between">
        <h2 id={headingId} className="ov-card__title">
          {t('security.sessions.title')}
        </h2>
        <Button variant="danger" lock disabled={busy} onClick={signOutEverywhere}>
          {t('security.sessions.signOutEverywhere')}
        </Button>
      </div>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {rows === null ? (
        error === null ? <p className="ov-muted">{t('common.loading')}</p> : null
      ) : (
        <div className="ov-tbl-wrap">
          <table className="ov-tbl">
            <thead>
              <tr>
                <th scope="col">{t('security.sessions.device')}</th>
                <th scope="col">{t('security.sessions.place')}</th>
                <th scope="col">{t('security.sessions.signedIn')}</th>
                <th scope="col">{t('security.sessions.lastActive')}</th>
                <th scope="col">
                  <span className="ov-sr">{t('security.sessions.action')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={row.current ? 'ov-tbl__sel' : undefined}>
                  <td>
                    <strong>{deviceName(row.userAgent)}</strong>
                    {row.current ? <StatePill tone="ok">{t('security.sessions.thisDevice')}</StatePill> : null}
                  </td>
                  <td>{row.ip !== null ? <span className="ov-mono">{row.ip}</span> : t('security.sessions.unknown')}</td>
                  <td className="ov-num">{signedInAt(row.createdAt)}</td>
                  <td>{row.current ? t('common.now') : lastActive(row.lastSeenAt)}</td>
                  <td>
                    {row.current ? null : (
                      <Button
                        disabled={busy}
                        aria-label={t('security.sessions.signOutDevice', { device: deviceName(row.userAgent) })}
                        onClick={() => {
                          signOutOne(row.id)
                        }}
                      >
                        {t('common.signOut')}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="ov-small ov-muted">{t('security.sessions.expiry')}</p>
    </section>
  )
}
