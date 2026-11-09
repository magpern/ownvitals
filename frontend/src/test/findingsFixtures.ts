// Synthetic answers of /api/findings and /api/findings/{id} for the findings screen's tests: made
// up for the tests (synthetic: true), not read from any account.

export const SYNTHETIC = true

const DAY_MS = 86_400_000
/** A local day `back` days before today, so a test never depends on the date it runs. */
export const daysAgo = (back: number): string => new Date(Date.now() - back * DAY_MS).toISOString().slice(0, 10)

export function syntheticFinding(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'f-trend',
    rule_id: 'B1',
    rule_version: 1,
    tier: 'trend',
    area: 'body',
    severity: 'info',
    confidence: 'medium',
    origin: 'live',
    status: 'active',
    subject: 'weight',
    metric_keys: ['weight'],
    window_start: daysAgo(12),
    window_end: daysAgo(9),
    reference_day: daysAgo(9),
    first_seen: `${daysAgo(9)}T06:00:00Z`,
    last_seen: `${daysAgo(9)}T06:00:00Z`,
    ended_at: null,
    seen_at: null,
    data_quality: { n_days: 11, window_days: 14, coverage: 0.8, longest_gap_days: 2 },
    not_evaluated: false,
    gate: null,
    why_key: 'b1',
    isnt_key: 'trend',
    ...over,
  }
}

export const LIVE_TREND = syntheticFinding()
export const LIVE_DEVICE = syntheticFinding({ id: 'f-device', rule_id: 'E2', tier: 'device', area: 'heart', seen_at: '2026-11-01T08:00:00Z' })
export const LIVE_NOT_EVALUATED = syntheticFinding({ id: 'f-e4', rule_id: 'E4', tier: 'device', area: 'heart', not_evaluated: true })
export const BACKFILL_TREND = syntheticFinding({ id: 'f-old', rule_id: 'B2', origin: 'backfill', window_start: daysAgo(200), window_end: daysAgo(30), seen_at: '2026-11-01T08:00:00Z' })
export const EXPERIMENTAL = syntheticFinding({ id: 'f-exp', rule_id: 'B5', tier: 'experimental' })

export const page = (findings: unknown[], next: number | null = null) => ({ findings, next_offset: next })
export const detail = (finding: Record<string, unknown>, evidence: Record<string, unknown> = {}) => ({ finding, evidence, readings: [], estimates: [] })
