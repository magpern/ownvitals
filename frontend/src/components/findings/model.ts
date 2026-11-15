import { AREAS, TABS, TIERS, type Area, type Filters, type FindingTier, type Tab } from '../../api/findings'
import type { JsonObject } from '../../api/decode'
import type { Range } from '../../api/metrics'

// What the findings screen decides, as pure functions: the filters in the address, which findings a
// tab may show, the numbers a finding's text is worded from, and the stretch of days the evidence
// chart shows. No clock, no I/O: `today` is passed in.

const isTab = (value: string | null): value is Tab => TABS.some((t) => t === value)
const isTier = (value: string | null): value is FindingTier => TIERS.some((t) => t === value)
const isArea = (value: string | null): value is Area => AREAS.some((a) => a === value)

export interface Location {
  filters: Filters
  /** The finding chosen in the list; `null` selects the first one shown. */
  id: string | null
}

/** The filters and the chosen finding, as the address holds them; a value it does not know is dropped. */
export function readLocation(query: URLSearchParams): Location {
  const tab = query.get('tab')
  const tier = query.get('tier')
  const area = query.get('area')
  return {
    filters: { tab: isTab(tab) ? tab : 'live', tier: isTier(tier) ? tier : null, area: isArea(area) ? area : null },
    id: query.get('id') || null,
  }
}

/** The address for a location: defaults are left out, so `/findings` is the live tab with no filter. */
export function locationUrl({ filters, id }: Location): string {
  const query = new URLSearchParams()
  if (filters.tab !== 'live') query.set('tab', filters.tab)
  if (filters.tier !== null) query.set('tier', filters.tier)
  if (filters.area !== null) query.set('area', filters.area)
  if (id !== null) query.set('id', id)
  const text = query.toString()
  return text === '' ? '/findings' : `/findings?${text}`
}

/** A finding made while importing history appears only under History, whatever a list holds. */
export function belongsToTab<T extends { origin: string }>(finding: T, tab: Tab): boolean {
  return (finding.origin === 'backfill') === (tab === 'history')
}

/** The numbers a rule's text (`health.rule.<id>.text`) is worded from; see docs/dev/i18n.md. */
export function evidenceParams(evidence: JsonObject): Record<string, number> {
  const pick = (name: string, ...keys: string[]): [string, number][] => {
    for (const key of keys) {
      const value = evidence[key]
      if (typeof value === 'number' && Number.isFinite(value)) return [[name, value]]
    }
    return []
  }
  return Object.fromEntries([
    ...pick('gainKg', 'gain_kg'),
    ...pick('windowDays', 'window_days'),
    ...pick('ratio', 'ratio', 'median_symmetry_index'),
    ...pick('threshold', 'threshold'),
    ...pick('bpm', 'margin_bpm'),
    ...pick('runDays', 'run_days', 'run_measure_days'),
    ...pick('days', 'days_since'),
    ...pick('count', 'count'),
    ...pick('total', 'total'),
  ])
}

const DAY_MS = 86_400_000
/** Days of context before the window that the chart shows. */
export const CONTEXT_DAYS = 28

function dayNumber(day: string): number {
  const [year, month, date] = day.split('-').map(Number)
  return Date.UTC(year ?? 1970, (month ?? 1) - 1, date ?? 1) / DAY_MS
}

/** A date as its local calendar day (issue 325: not the UTC day, which is off around midnight). */
export function localDay(date: Date): string {
  const two = (n: number) => String(n).padStart(2, '0')
  return `${String(date.getFullYear())}-${two(date.getMonth() + 1)}-${two(date.getDate())}`
}

/** The first day the evidence chart shows: some days before the window. */
export function chartStart(windowStart: string): string {
  return new Date((dayNumber(windowStart) - CONTEXT_DAYS) * DAY_MS).toISOString().slice(0, 10)
}

/** The shortest metric range that reaches back to the chart's first day (`today` is the local day). */
export function chartRange(windowStart: string, today: string): Range {
  const back = dayNumber(today) - dayNumber(chartStart(windowStart))
  if (back <= 30) return '30d'
  if (back <= 90) return '90d'
  if (back <= 182) return '6mo'
  if (back <= 365) return '1y'
  return 'all'
}

/** The points of a series inside the chart's stretch (the window, a day after it for the tail). */
export function inStretch<T extends { day: string }>(series: readonly T[], windowStart: string, windowEnd: string): T[] {
  const from = chartStart(windowStart)
  const to = new Date((dayNumber(windowEnd) + 1) * DAY_MS).toISOString().slice(0, 10)
  return series.filter((point) => point.day >= from && point.day <= to)
}
