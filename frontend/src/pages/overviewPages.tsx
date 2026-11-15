import type { ComponentType } from 'react'
import type { RouteId } from '../router/appRoutes'
import { ActivityPage, HeartPage, SleepPage } from './AreaPage'
import { OverviewPage } from './OverviewPage'

/** Issue 197: the overview and the heart, sleep and activity pages. */
export const OVERVIEW_PAGES: Partial<Record<RouteId, ComponentType>> = {
  overview: OverviewPage,
  heart: HeartPage,
  sleep: SleepPage,
  activity: ActivityPage,
}
