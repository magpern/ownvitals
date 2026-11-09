// Synthetic ECG API answers (no real recording, no real person). `synthetic: true` in spirit: the
// signal is a flat line with a 1 mV step, and every number is made up.

export function syntheticRecording(overrides: Record<string, unknown> = {}) {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    recorded_at: '2026-10-14T05:05:00Z',
    source_tz: 'Europe/Stockholm',
    recorded_local: '2026-10-14T07:05:00+02:00',
    device: { model_id: 10, name: 'Body Scan' },
    classification: { source: 'withings', code: 0, meaning: 'negative', details_code: 9, details_meaning: 'x' },
    heart_rate_bpm: 64,
    intervals: [
      { source: 'withings', key: 'qrs_duration', value: 94, unit: 'ms' },
      { source: 'withings', key: 'pr_interval', value: 162, unit: 'ms' },
    ],
    has_signal: true,
    ...overrides,
  }
}

export function syntheticSignal(hz = 100, seconds = 30) {
  const lead = Array.from({ length: hz * seconds }, (_, i) => (i >= hz ? 1000 : 0))
  return { sampling_hz: hz, unit: 'uV', lead_label: 'lead unverified', leads: [lead], lead_labels: ['lead unverified'] }
}

export function syntheticDetail(estimate: unknown = null, recording: Record<string, unknown> = {}) {
  return { recording: syntheticRecording(recording), wear_position: null, signal: syntheticSignal(), ownvitals_estimate: estimate }
}

export function syntheticEstimate(overrides: Record<string, unknown> = {}) {
  return {
    source: 'ownvitals_estimate',
    label_key: 'ecg.estimate.label',
    gate: 'pass',
    reasons: [],
    hr_bpm: 63.5,
    hrv: { rmssd_ms: 41, sdnn_ms: 52, pnn50_pct: 18 },
    rpeaks: [1.5, 2.4],
    windows: [],
    ...overrides,
  }
}
