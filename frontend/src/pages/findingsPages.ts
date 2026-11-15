import { createElement, lazy, Suspense, type ComponentType } from 'react'
import type { RouteId } from '../router/appRoutes'

// Issue 200: the findings inbox. It loads on first use: its evidence chart needs the chart
// library, which needs a canvas the moment it is imported.
const FindingsPage = lazy(() => import('./FindingsPage').then((m) => ({ default: m.FindingsPage })))

function Later() {
  return createElement(Suspense, { fallback: null }, createElement(FindingsPage))
}

export const FINDINGS_PAGES: Partial<Record<RouteId, ComponentType>> = { findings: Later }
