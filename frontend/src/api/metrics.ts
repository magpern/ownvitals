import { flag, object, optionalText, strings, text, type JsonObject } from './decode'
import { num, objects, optNum, optObject } from './metricDecode'

// The read API of the metric screens (backend `views/schemas.py`): a metric over a range, one day
// of it, one reading. Everything is checked as it is decoded: a day without a value is `null`
// (a gap), never 0; a number the API did not send is an error, not a zero.

export const RANGES = ['7d', '30d', '90d', '6mo', '1y', 'all'] as const
export type Range = (typeof RANGES)[number]
export const DEFAULT_RANGE: Range = '7d'

export function isRange(value: string | null): value is Range {
  return value !== null && (RANGES as readonly string[]).includes(value)
}

export interface Progress {
  daysWithData: number
  daysRequired: number
  spanDays: number
  spanRequired: number | null
  fraction: number
}

function progress(source: JsonObject | null): Progress | null {
  if (source === null) return null
  return {
    daysWithData: num(source, 'days_with_data'),
    daysRequired: num(source, 'days_required'),
    spanDays: num(source, 'span_days'),
    spanRequired: optNum(source, 'span_required'),
    fraction: num(source, 'fraction'),
  }
}

export interface Band {
  median: number
  low: number
  high: number
  windowDays: number
  k: number
}

export interface Latest {
  day: string
  value: number
  n: number
}

export interface SeriesPoint {
  day: string
  value: number | null
  n: number
}

export interface Trend {
  state: string
  progress: Progress | null
  direction: string | null
  significant: boolean | null
  slopePerDay: number | null
  nDays: number | null
}

export interface ChangePoint {
  day: string
  shift: number
}

export interface DataQuality {
  nDays: number
  windowDays: number
  coverage: number
  longestGapDays: number
}

export interface MethodInfo {
  code: string
  bandK: number
  bandWindowDays: number
  morningStart: string
  morningEnd: string
  algoVersions: string[]
}

export interface MetricDetail {
  key: string
  unit: string
  state: string
  range: string
  start: string
  end: string
  latest: Latest | null
  series: SeriesPoint[]
  unconfirmed: { day: string; n: number }[]
  band: Band | null
  bandProgress: Progress | null
  trend: Trend
  changePoints: { state: string; points: ChangePoint[] }
  dataQuality: DataQuality | null
  method: MethodInfo
  provenance: { algoVersions: string[]; inputRevision: number | null; recomputePending: boolean }
}

function decodeLatest(source: JsonObject | null): Latest | null {
  if (source === null) return null
  return { day: text(source, 'day'), value: num(source, 'value'), n: num(source, 'n') }
}

function decodeBand(source: JsonObject | null): Band | null {
  if (source === null) return null
  return {
    median: num(source, 'median'),
    low: num(source, 'low'),
    high: num(source, 'high'),
    windowDays: num(source, 'window_days'),
    k: num(source, 'k'),
  }
}

function decodeTrend(source: JsonObject): Trend {
  return {
    state: text(source, 'state'),
    progress: progress(optObject(source, 'progress')),
    direction: optionalText(source, 'direction'),
    significant: typeof source.significant === 'boolean' ? source.significant : null,
    slopePerDay: optNum(source, 'slope_per_day'),
    nDays: optNum(source, 'n_days'),
  }
}

function decodeQuality(source: JsonObject | null): DataQuality | null {
  if (source === null) return null
  return {
    nDays: num(source, 'n_days'),
    windowDays: num(source, 'window_days'),
    coverage: num(source, 'coverage'),
    longestGapDays: num(source, 'longest_gap_days'),
  }
}

function decodeMethod(source: JsonObject): MethodInfo {
  const parameters = object(source.parameters, 'parameters')
  return {
    code: text(source, 'code'),
    bandK: num(parameters, 'band_k'),
    bandWindowDays: num(parameters, 'band_window_days'),
    morningStart: text(parameters, 'morning_start'),
    morningEnd: text(parameters, 'morning_end'),
    algoVersions: Object.values(object(source.algo_versions, 'algo_versions')).filter(
      (v): v is string => typeof v === 'string',
    ),
  }
}

export function decodeMetricDetail(raw: unknown): MetricDetail {
  const s = object(raw, 'metric')
  const changes = object(s.change_points, 'change_points')
  const provenance = object(s.provenance, 'provenance')
  return {
    key: text(s, 'key'),
    unit: text(s, 'unit'),
    state: text(s, 'state'),
    range: text(s, 'range'),
    start: text(s, 'start'),
    end: text(s, 'end'),
    latest: decodeLatest(optObject(s, 'latest')),
    series: objects(s, 'series').map((p) => ({ day: text(p, 'day'), value: optNum(p, 'value'), n: num(p, 'n') })),
    unconfirmed: objects(s, 'unconfirmed').map((u) => ({ day: text(u, 'day'), n: num(u, 'n') })),
    band: decodeBand(optObject(s, 'band')),
    bandProgress: progress(optObject(s, 'band_progress')),
    trend: decodeTrend(object(s.trend, 'trend')),
    changePoints: {
      state: text(changes, 'state'),
      points: objects(changes, 'points').map((p) => ({ day: text(p, 'day'), shift: num(p, 'shift') })),
    },
    dataQuality: decodeQuality(optObject(s, 'data_quality')),
    method: decodeMethod(object(s.method, 'method')),
    provenance: {
      algoVersions: strings(provenance, 'algo_versions'),
      inputRevision: optNum(provenance, 'input_revision'),
      recomputePending: flag(provenance, 'recompute_pending'),
    },
  }
}

const enc = encodeURIComponent
/** The tiles of the overview over the chosen range (`all` and the rest as the metric screen's). */
export const metricsPath = (range: string): string => `/api/metrics?range=${enc(range)}`
export const metricPath = (key: string, range: Range): string => `/api/metrics/${enc(key)}?range=${range}`
export const dayPath = (key: string, day: string): string => `/api/days/${enc(key)}/${enc(day)}`
export const readingPath = (id: string): string => `/api/readings/${enc(id)}`

/** The in-app addresses: the metric, a day of it, and a reading opened over the day. */
export const metricUrl = (key: string, range?: Range): string =>
  `/m/${enc(key)}${range === undefined ? '' : `?range=${range}`}`
export const dayUrl = (key: string, day: string, reading?: string): string =>
  `/m/${enc(key)}/${enc(day)}${reading === undefined ? '' : `?reading=${enc(reading)}`}`
