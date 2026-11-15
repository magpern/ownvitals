import { isMessageKey, useT } from '../../i18n'
import type { DataStateKind } from '../ui/DataState'

/** A metric's name in the current language; a key the catalogue does not know is shown as it is. */
export function useMetricName(): (key: string) => string {
  const t = useT()
  return (key) => {
    const message = `detail.metric.${key}`
    return isMessageKey(message) ? t(message) : key
  }
}

const KINDS: Record<string, DataStateKind> = {
  up_to_date: 'up-to-date',
  stale: 'stale',
  importing: 'importing',
  incomplete: 'incomplete-import',
  insufficient_data: 'insufficient-data',
  not_supported: 'not-supported',
  not_shared: 'not-shared',
  not_available: 'not-available',
  needs_reconnect: 'needs-reconnect',
  withings_plus_needed: 'withings-plus',
  // Not connected reads as "not available": the connections screen says why and how to fix it.
  not_connected: 'not-available',
}

/** The data state of the design for the API's state; a state this build does not know reads as not available. */
export function dataStateKind(state: string): DataStateKind {
  return KINDS[state] ?? 'not-available'
}

/** States that say what the stream is: nothing computed from it is shown (the API's `BLOCKED`). */
export const BLOCKED_STATES: ReadonlySet<string> = new Set([
  'not_supported',
  'not_shared',
  'not_available',
  'needs_reconnect',
  'withings_plus_needed',
  'not_connected',
])
