import type { ReactNode } from 'react'
import { Card, ComingLater } from '../../components/ui/Card'
import { useT } from '../../i18n'
import type { AdminTab } from './AdminTabs'
import { AuditView } from './AuditView'
import { FlagsView } from './FlagsView'
import { JobsView } from './JobsView'

// The body of the tabs. Overview is the whole page of the mock-up: the tiles, Accounts, Jobs, the
// Withings app, Flags and the Audit log, each the same component its own tab shows. The tiles and
// the Withings app wait for their data (#549); Accounts is rendered by the page and passed in.

export function AdminSection({ tab }: { tab: Exclude<AdminTab, 'accounts' | 'overview'> }) {
  switch (tab) {
    case 'jobs':
      return <JobsView />
    case 'flags':
      return <FlagsView />
    case 'audit':
      return <AuditView />
    case 'withings':
      return <WithingsCard />
  }
}

function WithingsCard() {
  const t = useT()
  return (
    <Card>
      <ComingLater label={t('admin.withings.label')}>{t('admin.withings.later')}</ComingLater>
    </Card>
  )
}

/** Jobs | Withings app and Flags | Audit log side by side on wide screens, stacked on a phone. */
export function Overview({ accounts }: { accounts: ReactNode }) {
  const t = useT()
  return (
    <>
      <Card>
        <ComingLater label={t('admin.overview.label')}>{t('admin.overview.later')}</ComingLater>
      </Card>
      {accounts}
      <div className="ov-admin-pair">
        <JobsView />
        <WithingsCard />
      </div>
      <div className="ov-admin-pair">
        <FlagsView />
        <AuditView />
      </div>
    </>
  )
}
