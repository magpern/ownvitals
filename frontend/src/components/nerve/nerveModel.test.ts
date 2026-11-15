import { describe, expect, it } from 'vitest'
import type { Finding } from '../../api/findings'
import type { NerveMonth } from '../../api/nerve'
import { hasReadings, monthMetric, nerveFindings } from './nerveModel'

// Synthetic values (synthetic: true), made up for the tests.

const finding = (ruleId: string) => ({ id: ruleId, ruleId }) as unknown as Finding
const month = (days: number): NerveMonth => ({
  month: '2026-10',
  metrics: [{ metricKey: 'esc', daysWithData: days, median: days === 0 ? null : 71, minimum: null, maximum: null }],
})

describe('the nerve model', () => {
  it('keeps N2 (with its variants), N3 and N5 and nothing else, never N4', () => {
    const kept = nerveFindings(['N2:guided', 'N2:routine', 'N3', 'N4', 'N5', 'B2', 'N1'].map(finding)).map((f) => f.ruleId)
    expect(kept).toEqual(['N2:guided', 'N2:routine', 'N3', 'N5'])
  })

  it('finds a month metric or none, and a month with no data day has no readings', () => {
    expect(monthMetric(month(3), 'esc')?.median).toBe(71)
    expect(monthMetric(month(3), 'nerve_health_score')).toBeNull()
    expect(hasReadings(month(3))).toBe(true)
    expect(hasReadings(month(0))).toBe(false)
  })
})
