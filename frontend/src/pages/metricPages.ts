import { createElement, lazy, Suspense, type ComponentType, type LazyExoticComponent } from 'react'
import type { RouteId } from '../router/appRoutes'
import '../styles/metric.css'

// The metric screens, spread into `BUILT` in appPages.tsx with one line. They load on first use:
// the chart library is only needed here, and it needs a canvas the moment it is imported.

function later(component: LazyExoticComponent<ComponentType>): ComponentType {
  return function Later() {
    return createElement(Suspense, { fallback: null }, createElement(component))
  }
}

const MetricPage = lazy(() => import('./MetricPage').then((m) => ({ default: m.MetricPage })))
const DayPage = lazy(() => import('./DayPage').then((m) => ({ default: m.DayPage })))

export const METRIC_PAGES: Partial<Record<RouteId, ComponentType>> = { metric: later(MetricPage), day: later(DayPage) }
