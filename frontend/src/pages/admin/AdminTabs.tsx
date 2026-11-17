import { useT, type MessageKey } from '../../i18n'
import { Link, useQuery } from '../../router/Router'

// The tab row of the Admin mock-up. The tab lives in the address (?tab=jobs), so a tab can be
// linked to and the back button undoes it. An unknown value is the first tab.

export const ADMIN_TABS = ['overview', 'accounts', 'jobs', 'withings', 'flags', 'audit'] as const
export type AdminTab = (typeof ADMIN_TABS)[number]

export function useAdminTab(): AdminTab {
  const value = useQuery().get('tab')
  return ADMIN_TABS.find((tab) => tab === value) ?? 'overview'
}

export function AdminTabs({ active }: { active: AdminTab }) {
  const t = useT()
  return (
    <nav className="ov-tabs" aria-label={t('admin.tabs.label')}>
      {ADMIN_TABS.map((tab) => (
        <Link key={tab} to={`/admin?tab=${tab}`} aria-current={tab === active ? 'page' : undefined}>
          {t(`admin.tab.${tab}` as MessageKey)}
        </Link>
      ))}
    </nav>
  )
}
