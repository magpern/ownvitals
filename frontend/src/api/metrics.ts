import { DecodeError, flag, isObject, list, object, optionalText, strings, text, type JsonObject } from './decode'

// The read API of the metric screens (backend `views/schemas.py`): a metric over a range, one day
// of it, one reading. Everything is checked as it is decoded: a day without a value is `null`
// (a gap), never 0; a number the API did not send is an error, not a zero.

export const RANGES = ['30d', '90d', '6mo', '1y', 'all'] as const
export type Range = (typeof RANGES)[number]
export const DEFAULT_RANGE: Range = '90d'

export function isRange(value: string | null): value is Range {
  return value !== null && (RANGES as readonly string[]).includes(value)
}

function num(source: JsonObject, key: string): number {
  const value = source[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new DecodeError(key)
  return value
}

function optNum(source: JsonObject, key: string): number | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new DecodeError(key)
  return value
}

function optObject(source: JsonObject, key: string): JsonObject | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (!isObject(value)) throw new DecodeError(key)
  return value
}

function objects(source: JsonObject, key: string): JsonObject[] {
  return list(source, key).map((item) => object(item, key))
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

export interface DayReading {
  id: string
  localTime: string
  value: number
  qualityFlags: string[]
  used: boolean
}

export interface DayView {
  key: string
  day: string
  unit: string
  daily: { value: number; method: string; n: number; minimum: number; maximum: number } | null
  readings: DayReading[]
  alsoRecorded: { metricKey: string; nReadings: number }[]
}

export function decodeDay(raw: unknown): DayView {
  const s = object(raw, 'day')
  const daily = optObject(s, 'daily')
  return {
    key: text(s, 'key'),
    day: text(s, 'day'),
    unit: text(s, 'unit'),
    daily:
      daily === null
        ? null
        : {
            value: num(daily, 'value'),
            method: text(daily, 'method'),
            n: num(daily, 'n'),
            minimum: num(daily, 'minimum'),
            maximum: num(daily, 'maximum'),
          },
    readings: objects(s, 'readings').map((r) => ({
      id: text(r, 'id'),
      localTime: text(r, 'local_time'),
      value: num(r, 'value'),
      qualityFlags: strings(r, 'quality_flags'),
      used: flag(r, 'used'),
    })),
    alsoRecorded: objects(s, 'also_recorded').map((a) => ({
      metricKey: text(a, 'metric_key'),
      nReadings: num(a, 'n_readings'),
    })),
  }
}

export interface Gate {
  gate: string
  passed: boolean
  advisory: boolean
}

export interface ReadingView {
  id: string
  metricKey: string
  unit: string
  at: string
  sourceTz: string
  localDay: string
  value: number
  valueExact: string
  raw: { valueRaw: number; unitExponent: number }
  attrib: number | null
  device: { type: string | null; firmware: string | null } | null
  qualityFlags: string[]
  gates: Gate[]
  usedInDaily: boolean
  provenance: { algoVersion: string; inputRevision: number; recomputePending: boolean }
}

export function decodeReading(raw: unknown): ReadingView {
  const s = object(raw, 'reading')
  const rawOut = object(s.raw, 'raw')
  const device = optObject(s, 'device')
  const provenance = object(s.provenance, 'provenance')
  return {
    id: text(s, 'id'),
    metricKey: text(s, 'metric_key'),
    unit: text(s, 'unit'),
    at: text(s, 'at'),
    sourceTz: text(s, 'source_tz'),
    localDay: text(s, 'local_day'),
    value: num(s, 'value'),
    valueExact: text(s, 'value_exact'),
    raw: { valueRaw: num(rawOut, 'value_raw'), unitExponent: num(rawOut, 'unit_exponent') },
    attrib: optNum(s, 'attrib'),
    device: device === null ? null : { type: optionalText(device, 'type'), firmware: optionalText(device, 'firmware') },
    qualityFlags: strings(s, 'quality_flags'),
    gates: objects(s, 'gates').map((g) => ({ gate: text(g, 'gate'), passed: flag(g, 'passed'), advisory: flag(g, 'advisory') })),
    usedInDaily: flag(s, 'used_in_daily'),
    provenance: {
      algoVersion: text(provenance, 'algo_version'),
      inputRevision: num(provenance, 'input_revision'),
      recomputePending: flag(provenance, 'recompute_pending'),
    },
  }
}

const enc = encodeURIComponent
export const metricPath = (key: string, range: Range): string => `/api/metrics/${enc(key)}?range=${range}`
export const dayPath = (key: string, day: string): string => `/api/days/${enc(key)}/${enc(day)}`
export const readingPath = (id: string): string => `/api/readings/${enc(id)}`

/** The in-app addresses: the metric, a day of it, and a reading opened over the day. */
export const metricUrl = (key: string, range?: Range): string =>
  `/m/${enc(key)}${range === undefined || range === DEFAULT_RANGE ? '' : `?range=${range}`}`
export const dayUrl = (key: string, day: string, reading?: string): string =>
  `/m/${enc(key)}/${enc(day)}${reading === undefined ? '' : `?reading=${enc(reading)}`}`
