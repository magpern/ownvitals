import { useT } from '../../i18n'
import { Link, useRouter } from '../../router/Router'

// The four tabs of Settings (docs/design/mockups/src/Settings.dc.html, `.tabs`). /settings shows
// the first one, like /settings/profile.

const TABS = [
  { to: '/settings/profile', key: 'settings.tabs.profile', also: ['/settings'] },
  { to: '/settings/security', key: 'settings.tabs.security', also: [] },
  { to: '/settings/notifications', key: 'settings.tabs.notifications', also: [] },
  { to: '/settings/data', key: 'settings.tabs.data', also: [] },
] as const

export function SettingsTabs() {
  const t = useT()
  const { pathname } = useRouter()
  return (
    <nav className="ov-tabs" aria-label={t('settings.tabs.label')}>
      {TABS.map((tab) => {
        const current = pathname === tab.to || (tab.also as readonly string[]).includes(pathname)
        return (
          <Link key={tab.to} to={tab.to} aria-current={current ? 'page' : undefined}>
            {t(tab.key)}
          </Link>
        )
      })}
    </nav>
  )
}
