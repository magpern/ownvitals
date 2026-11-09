// Synthetic payloads of the metric screens (synthetic: true). Made-up numbers, no real account.

export const SYNTHETIC = true
export const FINGERPRINT = '9f3c41d0aa11bb22cc33dd44ee55ff667788990011223344556677887be2a71e'

type Json = Record<string, unknown>

export function syntheticMetric(over: Json = {}): Json {
  return {
    key: 'weight',
    unit: 'kg',
    state: 'up_to_date',
    range: '90d',
    start: '2026-10-01',
    end: '2026-10-03',
    latest: { day: '2026-10-03', value: 78.3, n: 1 },
    series: [
      { day: '2026-10-01', value: 78.1, n: 1 },
      { day: '2026-10-02', value: null, n: 0 },
      { day: '2026-10-03', value: 78.3, n: 1 },
    ],
    unconfirmed: [{ day: '2026-10-02', n: 1 }],
    band: { median: 78.2, low: 77.5, high: 78.9, window_days: 28, k: 2 },
    band_progress: null,
    trend: {
      state: 'ok',
      progress: null,
      direction: 'increasing',
      significant: true,
      slope_per_day: 0.01,
      n_days: 20,
    },
    change_points: { state: 'ready', points: [] },
    data_quality: { n_days: 2, window_days: 90, coverage: 0.02, longest_gap_days: 1 },
    method: {
      code: 'daily_median',
      parameters: { band_k: 2, band_window_days: 28, morning_start: '05:00', morning_end: '10:00' },
      algo_versions: { band: 'v1' },
    },
    provenance: { algo_versions: ['v1'], input_revision: 3, recompute_pending: false },
    ...over,
  }
}

export function syntheticInsufficient(): Json {
  const progress = { days_with_data: 2, days_required: 14, span_days: 2, span_required: 30, fraction: 0.14 }
  return syntheticMetric({
    state: 'insufficient_data',
    band: null,
    band_progress: progress,
    trend: { state: 'insufficient_data', progress, direction: null, significant: null, slope_per_day: null, n_days: null },
  })
}

export function syntheticDay(): Json {
  return {
    key: 'weight',
    day: '2026-10-02',
    unit: 'kg',
    daily: null,
    readings: [
      { id: 'r-unconfirmed', local_time: '07:10', value: 99.9, quality_flags: ['unconfirmed'], used: false },
      { id: 'r-used', local_time: '07:40', value: 78.2, quality_flags: [], used: true },
    ],
    also_recorded: [{ metric_key: 'fat_mass', n_readings: 2 }],
  }
}

export function syntheticReading(): Json {
  return {
    id: 'r-used',
    metric_key: 'weight',
    unit: 'kg',
    at: '2026-10-02T05:40:00Z',
    source_tz: 'Europe/Stockholm',
    local_day: '2026-10-02',
    value: 78.2,
    value_exact: '78.20',
    raw: { value_raw: 7820, unit_exponent: -2 },
    attrib: 0,
    device: { type: 'scale', firmware: '1.0' },
    quality_flags: [],
    gates: [{ gate: 'range', passed: true, advisory: false }],
    used_in_daily: true,
    provenance: { algo_version: 'v1', input_revision: 3, input_fingerprint: FINGERPRINT, recompute_pending: false },
  }
}

/** A trend that is not ready (state `insufficient_data`) although a slope is present: it must not be shown. */
export function syntheticTrendNotReady(): Json {
  return syntheticMetric({
    trend: {
      state: 'insufficient_data',
      progress: { days_with_data: 5, days_required: 14, span_days: 5, span_required: 30, fraction: 0.35 },
      direction: 'increasing',
      significant: true,
      slope_per_day: 0.5,
      n_days: 5,
    },
  })
}
