import type { DataStateKind } from '../ui/DataState'

// Pure rules of the Connections and Onboarding screens: facts in, a word out (no I/O).

/** A stream state of /api/status as a data state of the design; `null` for one we have no word for. */
export function stateKind(state: string): DataStateKind | null {
  switch (state) {
    case 'up_to_date':
      return 'up-to-date'
    case 'stale':
      return 'stale'
    case 'importing':
      return 'importing'
    case 'incomplete':
      return 'incomplete-import'
    case 'not_supported':
      return 'not-supported'
    case 'not_shared':
      return 'not-shared'
    case 'not_available':
      return 'not-available'
    case 'needs_reconnect':
      return 'needs-reconnect'
    case 'withings_plus_needed':
      return 'withings-plus'
    default:
      return null
  }
}

/** A stream Withings does not give us: never drawn as zero, never "available". */
export function isBlocked(state: string): boolean {
  return state === 'not_supported' || state === 'not_shared' || state === 'not_available'
}

/** What is still coming in: the screen keeps reading while this holds. No streams yet on a connected account means the first import has not started reporting. */
export function stillImporting(streams: readonly { state: string }[]): boolean {
  if (streams.length === 0) return true
  return streams.some((s) => s.state === 'importing' || stateKind(s.state) === null)
}
