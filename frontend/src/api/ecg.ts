import { count, list, object, optionalCount, optionalText, text, DecodeError, isObject, type JsonObject } from './decode'

// The ECG API (issue 199; backend/src/ownvitals/views/ecg_routes.py, docs/dev/ecg.md). Withings'
// values carry no label of ours: the page words them "Withings ...". A missing value is `null`
// and reads "not reported", never 0. The estimate is `null` unless the owner-only switch is on.

export const INTERVAL_KEYS = ['qrs_duration', 'pr_interval', 'qt_interval', 'qtc_interval'] as const
export type IntervalKey = (typeof INTERVAL_KEYS)[number]

export interface Recording {
  id: string
  recordedAt: string
  recordedLocal: string
  sourceTz: string
  deviceModelId: number | null
  deviceName: string | null
  afib: number | null
  afibDetails: number | null
  heartRate: number | null
  intervals: Partial<Record<IntervalKey, number>>
  hasSignal: boolean
}

export interface Signal {
  samplingHz: number
  /** Microvolts per lead, as stored. */
  leads: number[][]
}

export interface Estimate {
  gate: string | null
  reasons: string[]
  hrBpm: number | null
  hrv: { rmssd: number | null; sdnn: number | null; pnn50: number | null } | null
  /** R-peak positions in samples at the signal's rate (ADR 0006 section 8), not seconds. */
  rpeaks: number[] | null
}

export interface RecordingDetail {
  recording: Recording
  signal: Signal | null
  estimate: Estimate | null
}

export interface RecordingPage {
  recordings: Recording[]
  nextOffset: number | null
}

function finite(source: JsonObject, key: string): number | null {
  const value = optionalCount(source, key)
  return value !== null && Number.isFinite(value) ? value : null
}

function numbers(source: JsonObject, key: string): number[] {
  return list(source, key).map((x) => {
    if (typeof x !== 'number') throw new DecodeError(key)
    return x
  })
}

export function decodeRecording(raw: unknown): Recording {
  const r = object(raw, 'recording')
  const device = object(r.device, 'device')
  const classification = object(r.classification, 'classification')
  const intervals: Partial<Record<IntervalKey, number>> = {}
  for (const item of list(r, 'intervals')) {
    const i = object(item, 'interval')
    const key = text(i, 'key')
    const known = INTERVAL_KEYS.find((k) => k === key)
    if (known !== undefined) intervals[known] = count(i, 'value')
  }
  return {
    id: text(r, 'id'),
    recordedAt: text(r, 'recorded_at'),
    recordedLocal: text(r, 'recorded_local'),
    sourceTz: text(r, 'source_tz'),
    deviceModelId: optionalCount(device, 'model_id'),
    deviceName: optionalText(device, 'name'),
    afib: optionalCount(classification, 'code'),
    afibDetails: optionalCount(classification, 'details_code'),
    heartRate: optionalCount(r, 'heart_rate_bpm'),
    intervals,
    hasSignal: r.has_signal === true,
  }
}

export function decodeRecordingPage(raw: unknown): RecordingPage {
  const page = object(raw, 'recordings')
  return { recordings: list(page, 'recordings').map(decodeRecording), nextOffset: optionalCount(page, 'next_offset') }
}

function decodeEstimate(raw: unknown): Estimate | null {
  if (raw === null || raw === undefined) return null
  const e = object(raw, 'estimate')
  const hrv = isObject(e.hrv)
    ? { rmssd: finite(e.hrv, 'rmssd_ms'), sdnn: finite(e.hrv, 'sdnn_ms'), pnn50: finite(e.hrv, 'pnn50_pct') }
    : null
  return {
    gate: optionalText(e, 'gate'),
    reasons: Array.isArray(e.reasons) ? e.reasons.filter((x): x is string => typeof x === 'string') : [],
    hrBpm: finite(e, 'hr_bpm'),
    hrv,
    rpeaks: Array.isArray(e.rpeaks) ? numbers(e, 'rpeaks') : null,
  }
}

export function decodeRecordingDetail(raw: unknown): RecordingDetail {
  const d = object(raw, 'recording')
  const s = d.signal === null || d.signal === undefined ? null : object(d.signal, 'signal')
  const signal = s === null ? null : { samplingHz: count(s, 'sampling_hz'), leads: list(s, 'leads').map((l) => numbers({ l }, 'l')) }
  return {
    recording: decodeRecording(d.recording),
    signal: signal !== null && signal.samplingHz > 0 && signal.leads.length > 0 ? signal : null,
    estimate: decodeEstimate(d.ownvitals_estimate),
  }
}

export const ecgListPath = (offset: number) => `/api/ecg?offset=${String(offset)}`
export const ecgPath = (id: string) => `/api/ecg/${encodeURIComponent(id)}`
export const ecgPdfPath = (id: string) => `${ecgPath(id)}.pdf`
