import type { ComponentType } from 'react'
import type { AppRoute, RouteId } from '../router/appRoutes'
import { Card, ComingLater } from '../components/ui/Card'
import { useT } from '../i18n'
import { AdminPage } from './AdminPage'
import { BodyPage } from './BodyPage'
import { ConnectionsPage } from './ConnectionsPage'
import { ECG_PAGES } from './ecgPages'
import { FINDINGS_PAGES } from './findingsPages'
import { OVERVIEW_PAGES } from './overviewPages'
import { SecurityPage } from './SecurityPage'
import { METRIC_PAGES } from './metricPages'
import { SETTINGS_PAGES } from './settingsPages'

// The screens of the signed-in app. A screen that is not built yet shows the "Coming later"
// pattern under its page header (the frame supplies the header). The issue that builds a screen
// puts it here, and its placeholder goes.

/** Screens that are built, by route. */
const BUILT: Partial<Record<RouteId, ComponentType>> = {
  // Issue 127: the Accounts table. The rest of the admin mock-up stays "Coming later".
  admin: AdminPage,
  // #11: the lock band, "Where you're signed in" and the step-up dialog; the rest comes later.
  security: SecurityPage,
  // Issue 196: the Connections screen.
  connections: ConnectionsPage,
  // Issue 201: body composition.
  body: BodyPage,
  ...OVERVIEW_PAGES,
  ...METRIC_PAGES,
  ...ECG_PAGES,
  ...FINDINGS_PAGES,
  ...SETTINGS_PAGES,
}

function ComingLaterPage({ route }: { route: AppRoute }) {
  const t = useT()
  return (
    <Card>
      <ComingLater label={t(route.label ?? route.title)}>{t(route.later)}</ComingLater>
    </Card>
  )
}

export function AppPage({ route }: { route: AppRoute }) {
  const Page = BUILT[route.id]
  return Page ? <Page /> : <ComingLaterPage route={route} />
}
