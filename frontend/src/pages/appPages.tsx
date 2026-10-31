import type { ComponentType } from 'react'
import type { AppRoute, RouteId } from '../router/appRoutes'
import { Card, ComingLater } from '../components/ui/Card'

// The screens of the signed-in app. A screen that is not built yet shows the "Coming later"
// pattern under its page header (the frame supplies the header). The issue that builds a screen
// puts it here, and its placeholder goes.

/** Screens that are built, by route. */
const BUILT: Partial<Record<RouteId, ComponentType>> = {}

function ComingLaterPage({ route }: { route: AppRoute }) {
  return (
    <Card>
      <ComingLater label={route.label ?? route.title}>{route.later}</ComingLater>
    </Card>
  )
}

export function AppPage({ route }: { route: AppRoute }) {
  const Page = BUILT[route.id]
  return Page ? <Page /> : <ComingLaterPage route={route} />
}
