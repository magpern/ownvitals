import { count, DecodeError, list, object, optionalCount, optionalText, strings, text, type JsonObject } from './decode'

// The reads of the overview: /api/status (the freshness line, the stream tiles), /api/metrics (the
// tiles) and /api/findings (Needs a look). Each decoder checks what it uses; a field the screen
// does not show is not read. A number that may be missing stays `null`: it is never made a zero.

export interface StreamStatus {
  stream: string
  state: string
  /** Withings' answer for the stream: `supported`, `unsupported`, `not_authorized` ... */
  capability: string | null
  progressPercent: number | null
  lastOkAt: string | null
}

export interface Status {
  asOf: string
  /** `not_connected`, `connected`, `needs_reconnect` or `withings_plus_needed`. */
  connection: string
  lastReadingAt: string | null
  lastSyncAt: string | null
  streams: StreamStatus[]
  importing: string[]
  incomplete: string[]
  brokenConnections: string[]
}

function stream(raw: unknown): StreamStatus {
  const s = object(raw, 'stream')
  return {
    stream: text(s, 'stream'),
    state: text(s, 'state'),
    capability: optionalText(s, 'capability'),
    progressPercent: optionalCount(s, 'progress_percent'),
    lastOkAt: optionalText(s, 'last_ok_at'),
  }
}

export function decodeStatus(raw: unknown): Status {
  const s = object(raw, 'status')
  return {
    asOf: text(s, 'as_of'),
    connection: text(s, 'connection'),
    lastReadingAt: optionalText(s, 'last_reading_at'),
    lastSyncAt: optionalText(s, 'last_sync_at'),
    streams: list(s, 'streams').map(stream),
    importing: strings(s, 'importing'),
    incomplete: strings(s, 'incomplete'),
    brokenConnections: strings(s, 'broken_connections'),
  }
}

export interface Latest {
  day: string
  value: number
  n: number
}

export interface TypicalRange {
  low: number
  high: number
}

export interface SparkDay {
  day: string
  /** `null` on a day without a value. */
  value: number | null
}

export interface Progress {
  daysWithData: number
  daysRequired: number
}

export interface MetricTile {
  key: string
  area: string
  unit: string
  stream: string
  state: string
  progressPercent: number | null
  progress: Progress | null
  latest: Latest | null
  typicalRange: TypicalRange | null
  sparkline: SparkDay[]
}

function present(source: JsonObject, key: string): JsonObject | null {
  const value = source[key]
  return value === undefined || value === null ? null : object(value, key)
}

function number(source: JsonObject, key: string): number {
  const value = source[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new DecodeError(key)
  return value
}

function tile(raw: unknown): MetricTile {
  const m = object(raw, 'metric')
  const latest = present(m, 'latest')
  const band = present(m, 'typical_range')
  const progress = present(m, 'progress')
  return {
    key: text(m, 'key'),
    area: text(m, 'area'),
    unit: text(m, 'unit'),
    stream: text(m, 'stream'),
    state: text(m, 'state'),
    progressPercent: optionalCount(m, 'progress_percent'),
    progress: progress && {
      daysWithData: count(progress, 'days_with_data'),
      daysRequired: count(progress, 'days_required'),
    },
    latest: latest && { day: text(latest, 'day'), value: number(latest, 'value'), n: count(latest, 'n') },
    typicalRange: band && { low: number(band, 'low'), high: number(band, 'high') },
    sparkline: list(m, 'sparkline').map((p) => {
      const point = object(p, 'sparkline')
      const value = point.value
      if (value !== null && value !== undefined && typeof value !== 'number') throw new DecodeError('value')
      return { day: text(point, 'day'), value: typeof value === 'number' ? value : null }
    }),
  }
}

export function decodeMetrics(raw: unknown): MetricTile[] {
  return list(object(raw, 'metrics'), 'metrics').map(tile)
}

export interface Finding {
  id: string
  ruleId: string
  tier: string
  area: string | null
  confidence: string
  windowStart: string
  windowEnd: string
  /** Not yet looked at: the finding is "new". */
  isNew: boolean
  notEvaluated: boolean
}

export interface Findings {
  findings: Finding[]
  /** More findings than this page holds. */
  more: boolean
}

export function decodeFindings(raw: unknown): Findings {
  const f = object(raw, 'findings')
  return {
    findings: list(f, 'findings').map((item) => {
      const x = object(item, 'finding')
      return {
        id: text(x, 'id'),
        ruleId: text(x, 'rule_id'),
        tier: text(x, 'tier'),
        area: optionalText(x, 'area'),
        confidence: text(x, 'confidence'),
        windowStart: text(x, 'window_start'),
        windowEnd: text(x, 'window_end'),
        isNew: optionalText(x, 'seen_at') === null,
        notEvaluated: x.not_evaluated === true,
      }
    }),
    more: f.next_offset !== null && f.next_offset !== undefined,
  }
}
