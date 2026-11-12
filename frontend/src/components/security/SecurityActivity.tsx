import { ACTIVITY_PATH, decodeActivity, type ActivityEvent } from '../../api/securityActivity'
import { useResource } from '../../api/useResource'
import { getLocale, isMessageKey, t as translate, useT } from '../../i18n'
import type { Locale } from '../../i18n/locale'
import { browserZone, formatDateOfInstant, formatTime } from '../../lib/format'
import { Card } from '../ui/Card'
import { ResourceView } from '../ui/DataState'

// "Recent security activity" on Sign-in and security (docs/design/mockups/src/Security.dc.html;
// roadmap Amendment 7, H3): the last 30 days of GET /api/auth/activity. The server sends codes;
// every word is the catalogue's (`security.activity.*`). A kind the catalogue does not know gets a
// generic line, never a blank or a raw code.

const WINDOW_DAYS = 30
/** Events that did not succeed, and a session that looked misused: the mock-up's warning tone. */
const FAILURES = new Set(['sign_in_failed', 'reauth_failed', 'mfa_failed', 'session_anomaly'])

/** The event's wording: by code, and for a sign-in by method. */
export function eventLabel(item: Pick<ActivityEvent, 'kind' | 'method'>): string {
  const byMethod = item.kind === 'sign_in' && item.method !== null ? `security.activity.kind.sign_in.${item.method}` : null
  if (byMethod !== null && isMessageKey(byMethod)) return translate(byMethod)
  const key = `security.activity.kind.${item.kind}`
  return isMessageKey(key) ? translate(key) : translate('security.activity.kind.unknown')
}

/** "Firefox on Windows", "Firefox", "Windows" or "Unknown device". */
export function eventDevice(item: Pick<ActivityEvent, 'browser' | 'os'>): string {
  if (item.browser !== null && item.os !== null) return translate('security.device.on', { browser: item.browser, system: item.os })
  return item.browser ?? item.os ?? translate('security.device.unknown')
}

function eventPlace(place: string): string {
  return place === 'Place unknown' ? translate('security.activity.place.unknown') : place
}

/** "Today 07:30", "Yesterday 21:12" or "28 Oct", in the browser's zone. */
export function eventTime(iso: string, now: Date = new Date(), locale: Locale = getLocale()): string {
  const at = new Date(iso)
  const zone = browserZone()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (at.toDateString() === now.toDateString()) return translate('security.time.today', { time: formatTime(locale, at, zone) })
  if (at.toDateString() === yesterday.toDateString()) return translate('security.activity.yesterday', { time: formatTime(locale, at, zone) })
  return formatDateOfInstant(locale, at, zone)
}

export function withinWindow(events: ActivityEvent[], now: Date = new Date()): ActivityEvent[] {
  const since = now.getTime() - WINDOW_DAYS * 86_400_000
  return events.filter((item) => new Date(item.at).getTime() >= since)
}

export function SecurityActivity() {
  const t = useT()
  const resource = useResource(ACTIVITY_PATH, decodeActivity)
  return (
    <Card title={t('security.activity.title')} aside={<span className="ov-small ov-muted">{t('security.activity.window')}</span>}>
      <ResourceView resource={resource}>
        {(all) => {
          const events = withinWindow(all)
          if (events.length === 0) return <p className="ov-muted">{t('security.activity.empty')}</p>
          return (
            <ul className="ov-activity">
              {events.map((item, index) => (
                <li key={`${item.at}-${index}`} className="ov-setting" data-tone={FAILURES.has(item.kind) ? 'attn' : undefined}>
                  <div className="ov-setting__what">
                    <span className="ov-setting__label">{eventLabel(item)}</span>
                    <p>
                      {eventDevice(item)} · {eventPlace(item.place)}
                    </p>
                  </div>
                  <span className="ov-small ov-muted ov-num">{eventTime(item.at)}</span>
                </li>
              ))}
            </ul>
          )
        }}
      </ResourceView>
    </Card>
  )
}
