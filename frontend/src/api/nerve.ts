import { count, list, object, type JsonObject } from './decode'
import { DecodeError } from './decode'

// The read of the nerve page's monthly summary (/api/nerve/summary, ADR 0010 section 8). Only what
// the page shows is read; a median or extreme that may be missing stays `null`, never a zero.

export interface NerveMetricMonth {
  metricKey: string
  daysWithData: number
  median: number | null
  minimum: number | null
  maximum: number | null
}

export interface NerveMonth {
  /** `YYYY-MM`. */
  month: string
  metrics: NerveMetricMonth[]
}

export const NERVE_SUMMARY_PATH = '/api/nerve/summary'

function optionalNumber(source: JsonObject, key: string): number | null {
  const value = source[key]
  if (value === undefined || value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new DecodeError(key)
  return value
}

function metricMonth(raw: unknown): NerveMetricMonth {
  const m = object(raw, 'metric month')
  const key = m.metric_key
  if (typeof key !== 'string') throw new DecodeError('metric_key')
  return {
    metricKey: key,
    daysWithData: count(m, 'days_with_data'),
    median: optionalNumber(m, 'median'),
    minimum: optionalNumber(m, 'minimum'),
    maximum: optionalNumber(m, 'maximum'),
  }
}

export function decodeNerveSummary(raw: unknown): NerveMonth[] {
  return list(object(raw, 'nerve summary'), 'months').map((r) => {
    const m = object(r, 'month')
    const month = m.month
    if (typeof month !== 'string' || !/^\d{4}-\d{2}$/.test(month)) throw new DecodeError('month')
    return { month, metrics: list(m, 'metrics').map(metricMonth) }
  })
}
