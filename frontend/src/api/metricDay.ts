import { flag, object, optionalText, strings, text } from './decode'
import { num, objects, optNum, optObject } from './metricDecode'

// The day view and the reading (backend `views/schemas.py`): a day of a metric, one reading.

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
