import { describe, expect, it } from 'vitest'
import { dayUrl, decodeMetricDetail, isRange, metricUrl, readingPath } from './metrics'
import { syntheticMetric } from '../test/metricFixtures'

// Synthetic data only (see metricFixtures.ts).

describe('decodeMetricDetail', () => {
  it('keeps a day without a value as null, never 0', () => {
    const detail = decodeMetricDetail(syntheticMetric())
    expect(detail.series.map((p) => p.value)).toEqual([78.1, null, 78.3])
  })

  it('refuses a number the API did not send', () => {
    const raw = syntheticMetric()
    ;(raw.series as Record<string, unknown>[])[0] = { day: '2026-10-01', value: 'x', n: 1 }
    expect(() => decodeMetricDetail(raw)).toThrow()
  })
})

describe('addresses', () => {
  it('names the range in the metric address whenever one is chosen (the saved default may differ)', () => {
    expect(metricUrl('weight')).toBe('/m/weight')
    expect(metricUrl('weight', '90d')).toBe('/m/weight?range=90d')
    expect(metricUrl('weight', '1y')).toBe('/m/weight?range=1y')
  })

  it('opens a reading over its day', () => {
    expect(dayUrl('weight', '2026-10-03')).toBe('/m/weight/2026-10-03')
    expect(dayUrl('weight', '2026-10-03', 'r1')).toBe('/m/weight/2026-10-03?reading=r1')
    expect(readingPath('r 1')).toBe('/api/readings/r%201')
  })

  it('knows its ranges', () => {
    expect(isRange('30d')).toBe(true)
    expect(isRange('2d')).toBe(false)
    expect(isRange(null)).toBe(false)
  })
})
