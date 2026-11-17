import { Card, ComingLater } from '../../components/ui/Card'
import { useT } from '../../i18n'
import type { AdminTab } from './AdminTabs'
import { AuditView } from './AuditView'
import { FlagsView } from './FlagsView'
import { JobsView } from './JobsView'

// The body of the tabs this issue builds. Overview and Withings app wait for the data their
// mock-up cards show (#546 part 2); Accounts is rendered by the page.

export function AdminSection({ tab }: { tab: Exclude<AdminTab, 'accounts'> }) {
  const t = useT()
  switch (tab) {
    case 'jobs':
      return <JobsView />
    case 'flags':
      return <FlagsView />
    case 'audit':
      return <AuditView />
    case 'withings':
      return (
        <Card>
          <ComingLater label={t('admin.withings.label')}>{t('admin.withings.later')}</ComingLater>
        </Card>
      )
    case 'overview':
      return (
        <Card>
          <ComingLater label={t('admin.overview.label')}>{t('admin.overview.later')}</ComingLater>
        </Card>
      )
  }
}
