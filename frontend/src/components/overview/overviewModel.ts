import type { MetricTile, Status } from '../../api/overview'
import type { MessageKey } from '../../i18n/en'
import type { Params } from '../../i18n/translate'
import type { NavStatuses } from '../shell/Rail'
import type { DataStateKind } from '../ui/DataState'

// The rules of the overview, with no I/O: which state a tile is in and whether it may show a
// number, what the freshness line says, and what the rail marks. Text comes through the `tr`
// function the caller passes in; the clock comes in as `now`.

export type Translate = (key: MessageKey, params?: Params) => string

export const RANGES = ['30d', '90d', '1y', 'all'] as const
export type RangeId = (typeof RANGES)[number]
export const DEFAULT_RANGE: RangeId = '90d'

/** The `?range=` of the address, or the default for anything else. */
export function parseRange(value: string | null): RangeId {
  return RANGES.find((r) => r === value) ?? DEFAULT_RANGE
}

/** The data state of the design for an API state. `not_connected` reads as "not available". */
export function stateKind(state: string): DataStateKind | null {
  switch (state) {
    case 'up_to_date':
      return null
    case 'stale':
      return 'stale'
    case 'importing':
      return 'importing'
    case 'incomplete':
      return 'incomplete-import'
    case 'insufficient_data':
      return 'insufficient-data'
    case 'not_supported':
      return 'not-supported'
    case 'not_shared':
      return 'not-shared'
    case 'needs_reconnect':
      return 'needs-reconnect'
    case 'withings_plus_needed':
      return 'withings-plus'
    default:
      // not_available, not_connected, and a state this screen does not know: never a number.
      return 'not-available'
  }
}

/** Only a state that has readings may show a number, and only when the API sent one. */
export function showsValue(tile: Pick<MetricTile, 'state' | 'latest'>): boolean {
  return tile.latest !== null && ['up_to_date', 'stale', 'incomplete'].includes(tile.state)
}

/** The sentence under a state, by the API state (a catalogue key). */
export function detailKey(state: string): MessageKey {
  const known = [
    'not_supported',
    'not_shared',
    'not_available',
    'needs_reconnect',
    'withings_plus_needed',
    'not_connected',
    'importing',
    'incomplete',
    'stale',
  ]
  return known.includes(state) ? (`overview.tile.detail.${state}` as MessageKey) : 'overview.tile.detail.up_to_date'
}

/** Decimals of a metric's number (an index or a count is whole). */
export function digitsOf(key: string): number {
  return ['heart_pulse', 'vascular_age', 'visceral_fat', 'metabolic_age', 'nerve_health_score'].includes(key) ? 0 : 1
}

/** The tiles of each part of the overview, by metric key; blood pressure is one tile of two keys. */
export const BODY_KEYS = ['weight', 'fat_ratio', 'muscle_mass', 'visceral_fat'] as const
export const HEART_KEYS = ['heart_pulse', 'vascular_age', 'blood_pressure'] as const
export const NERVE_KEY = 'nerve_health_score'

/**
 * Systolic and diastolic as one tile: the systolic tile's state, no range or sparkline, and the
 * two latest values written together ("118/76"). Without both values there is no number.
 */
export function bloodPressure(tiles: MetricTile[]): { tile: MetricTile; value: string | null } | null {
  const sys = tiles.find((x) => x.key === 'systolic_bp')
  if (sys === undefined) return null
  const dia = tiles.find((x) => x.key === 'diastolic_bp')
  const both = sys.latest !== null && dia?.latest != null
  return {
    tile: { ...sys, key: 'blood_pressure', latest: both ? sys.latest : null, typicalRange: null, sparkline: [] },
    value: both && sys.latest !== null && dia?.latest != null ? `${Math.round(sys.latest.value)}/${Math.round(dia.latest.value)}` : null,
  }
}

/** Whole minutes between two instants, never negative. */
function minutesBetween(from: string, now: Date): number {
  return Math.max(0, Math.floor((now.getTime() - new Date(from).getTime()) / 60_000))
}

/** "just now", "12 min ago", "5 h ago", "3 d ago". */
export function agoText(from: string, now: Date, tr: Translate): string {
  const m = minutesBetween(from, now)
  if (m < 1) return tr('overview.ago.now')
  if (m < 60) return tr('overview.ago.minutes', { n: m })
  if (m < 60 * 48) return tr('overview.ago.hours', { n: Math.floor(m / 60) })
  return tr('overview.ago.days', { n: Math.floor(m / 1440) })
}

export const STALE_AFTER_HOURS = 48

export type FreshTone = 'ok' | 'attn' | 'urgent' | 'info' | 'off'
export interface FreshItem {
  id: string
  tone: FreshTone
  text: string
  /** The connection items offer a way to fix it. */
  fix?: 'connect'
}

export interface Freshness {
  /** What is said on the line itself. */
  lead: FreshItem[]
  /** The capability states, behind "Details". */
  details: FreshItem[]
}

/** The name of a stream, as the catalogue words it; a stream it does not know is left out. */
function streamName(id: string, tr: Translate, known: (key: string) => boolean): string | null {
  const key = `overview.stream.${id}`
  return known(key) ? tr(key as MessageKey) : null
}

function connectionItem(status: Status, now: Date, tr: Translate): FreshItem {
  switch (status.connection) {
    case 'not_connected':
      return { id: 'connection', tone: 'attn', text: tr('overview.fresh.notConnected'), fix: 'connect' }
    case 'needs_reconnect':
      return { id: 'connection', tone: 'urgent', text: tr('overview.fresh.needsReconnect'), fix: 'connect' }
    case 'withings_plus_needed':
      return { id: 'connection', tone: 'info', text: tr('overview.fresh.plusNeeded') }
    default: {
      if (status.lastSyncAt === null) return { id: 'connection', tone: 'attn', text: tr('overview.fresh.neverSynced') }
      const ago = agoText(status.lastSyncAt, now, tr)
      const stale = minutesBetween(status.lastSyncAt, now) > STALE_AFTER_HOURS * 60
      return stale
        ? { id: 'connection', tone: 'attn', text: tr('overview.fresh.stale', { ago }) }
        : { id: 'connection', tone: 'ok', text: tr('overview.fresh.synced', { ago }) }
    }
  }
}

/** What the freshness line says: the connection, the last reading, imports and what is missing. */
export function freshness(status: Status, now: Date, tr: Translate, known: (key: string) => boolean): Freshness {
  const lead = [connectionItem(status, now, tr)]
  const connected = status.connection === 'connected'
  if (connected && status.lastReadingAt !== null) {
    lead.push({ id: 'reading', tone: 'ok', text: tr('overview.fresh.lastReading', { when: agoText(status.lastReadingAt, now, tr) }) })
  }
  const details: FreshItem[] = []
  for (const s of status.streams) {
    const name = streamName(s.stream, tr, known)
    if (name === null) continue
    if (s.state === 'importing') {
      lead.push({ id: `import-${s.stream}`, tone: 'info', text: tr('overview.fresh.importing', { stream: name, percent: s.progressPercent ?? 0 }) })
    } else if (s.state === 'incomplete') {
      lead.push({ id: `incomplete-${s.stream}`, tone: 'attn', text: tr('overview.fresh.incomplete', { stream: name }) })
    } else if (s.state === 'not_supported' || s.state === 'not_shared' || s.state === 'not_available') {
      details.push({ id: `cap-${s.stream}`, tone: 'off', text: tr(`overview.fresh.stream.${s.state}`, { stream: name }) })
    }
  }
  return { lead, details }
}

/** The state of a stream by id, `null` when the status does not list it. */
export function streamState(status: Status, id: string): { state: string; percent: number | null; lastOkAt: string | null } | null {
  const s = status.streams.find((x) => x.stream === id)
  return s === undefined ? null : { state: s.state, percent: s.progressPercent, lastOkAt: s.lastOkAt }
}

/** The marks of the rail: the connection dot, and "n/a" on an area whose stream cannot be read. */
export function railStatus(status: Status, tr: Translate): NavStatuses {
  const good = status.connection === 'connected' && status.brokenConnections.length === 0
  const out: NavStatuses = {
    connections: { badge: { kind: 'dot', tone: good ? 'ok' : 'attn', label: tr(good ? 'overview.rail.connected' : 'overview.rail.attention') } },
  }
  const dim = (id: 'heart' | 'sleep' | 'activity', stream: string) => {
    const s = streamState(status, stream)
    if (s !== null && ['not_supported', 'not_shared', 'not_available'].includes(s.state)) {
      out[id] = { dim: true, badge: { kind: 'text', text: tr('overview.rail.notAvailable') } }
    }
  }
  dim('heart', 'ecg')
  dim('sleep', 'sleep_summary')
  dim('activity', 'activity_daily')
  return out
}
