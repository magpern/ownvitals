// Synthetic answers of /api/status, /api/metrics and /api/findings for the overview's tests: made
// up for the tests, not read from any account. Builders take what a test changes.

const NOW = '2026-11-01T12:00:00Z'

export interface FakeStream {
  stream: string
  state: string
  capability: string | null
  progress_percent: number | null
  last_ok_at: string | null
  skipped_out_of_range: number
}

export function fakeStream(stream: string, state = 'up_to_date', more: Partial<FakeStream> = {}): FakeStream {
  return {
    stream,
    state,
    capability: null,
    progress_percent: null,
    last_ok_at: state === 'up_to_date' ? '2026-11-01T11:30:00Z' : null,
    skipped_out_of_range: 0,
    ...more,
  }
}

const STREAMS = ['measures', 'ecg', 'activity_daily', 'workouts', 'sleep_summary', 'sleep_series', 'intraday']

export function fakeStatus(more: Record<string, unknown> = {}) {
  return {
    as_of: NOW,
    connection: 'connected',
    last_reading_at: '2026-11-01T07:00:00Z',
    last_sync_at: '2026-11-01T11:30:00Z',
    streams: STREAMS.map((s) => fakeStream(s)),
    importing: [],
    incomplete: [],
    broken_connections: [],
    ...more,
  }
}

/** Thirty synthetic days, the last `n` of them with a value around `base`. */
function spark(base: number, n = 30) {
  return Array.from({ length: 30 }, (_, i) => ({
    day: `2026-10-${String(i + 3).padStart(2, '0')}`,
    value: i >= 30 - n ? Number((base + (i % 3) * 0.1).toFixed(1)) : null,
  }))
}

export function fakeMetric(key: string, more: Record<string, unknown> = {}) {
  return {
    key,
    area: 'body',
    unit: 'kg',
    stream: 'measures',
    state: 'up_to_date',
    progress_percent: null,
    progress: null,
    latest: { day: '2026-11-01', value: 78.4, n: 1, method: 'last' },
    typical_range: { median: 78.5, low: 77.6, high: 79.3, half_width: 0.9, k: 2, window_days: 90, as_of: '2026-11-01' },
    sparkline: spark(78.4),
    ...more,
  }
}

export const NO_VALUE = { latest: null, typical_range: null, sparkline: [] }

export function fakeMetrics(metrics: unknown[] = []) {
  return { as_of: NOW, metrics }
}

export function fakeFinding(more: Record<string, unknown> = {}) {
  return {
    id: '00000000-0000-4000-8000-000000000001',
    rule_id: 'B1',
    rule_version: '1',
    tier: 'trend',
    area: 'body',
    severity: 'attention',
    confidence: 'medium',
    origin: 'rule',
    status: 'live',
    subject: 'weight',
    metric_keys: ['weight'],
    window_start: '2026-10-29',
    window_end: '2026-11-01',
    reference_day: null,
    first_seen: '2026-11-01T06:00:00Z',
    last_seen: '2026-11-01T06:00:00Z',
    ended_at: null,
    seen_at: null,
    data_quality: {},
    not_evaluated: false,
    gate: null,
    why_key: 'finding.why',
    isnt_key: 'finding.isnt',
    ...more,
  }
}

export function fakeFindings(findings: unknown[] = [], nextOffset: number | null = null) {
  return { findings, next_offset: nextOffset }
}

/** What the overview reads, answered as "connected, nothing yet": the defaults of `fakeApi`. */
export const OVERVIEW_ROUTES = {
  'GET /api/status': fakeStatus(),
  'GET /api/metrics': fakeMetrics(),
  'GET /api/findings?tab=live&limit=20': fakeFindings(),
}
