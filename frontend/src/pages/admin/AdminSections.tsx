import type { ReactNode } from 'react'
import { useT } from '../../i18n'
import type { AdminTab } from './AdminTabs'
import { AuditView } from './AuditView'
import { FlagsView } from './FlagsView'
import { JobsView } from './JobsView'
import { OverviewTiles } from './OverviewTiles'
import { WithingsAppCard } from './WithingsAppCard'

// The body of the tabs. Overview is the whole page of the mock-up: the tiles, Accounts, Jobs, the
// Withings app, Flags and the Audit log, each the same component its own tab shows. The tiles and
// the Withings app card are issue 549's; Accounts is rendered by the page and passed in.

export function AdminSection({ tab }: { tab: Exclude<AdminTab, 'accounts' | 'overview'> }) {
  switch (tab) {
    case 'jobs':
      return <JobsView />
    case 'flags':
      return <FlagsView />
    case 'audit':
      return <AuditView />
    case 'withings':
      return <WithingsAppCard />
  }
}

/** Jobs | Withings app and Flags | Audit log side by side on wide screens, stacked on a phone. */
export function Overview({ accounts }: { accounts: ReactNode }) {
  const t = useT()
  return (
    <>
      <OverviewTiles />
      <p className="ov-small ov-muted">{t('admin.overview.metrics')}</p>
      {accounts}
      <div className="ov-admin-pair">
        <JobsView />
        <WithingsAppCard />
      </div>
      <div className="ov-admin-pair">
        <FlagsView />
        <AuditView />
      </div>
    </>
  )
}
