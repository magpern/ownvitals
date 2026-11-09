import { describe, expect, it } from 'vitest'
import { chartPoints, longestHole, progressPercent, recentValues, smooth } from './metricData'

// Synthetic series.
const series = [
  { day: '2026-10-01', value: 80, n: 1 },
  { day: '2026-10-02', value: null, n: 0 },
  { day: '2026-10-03', value: 82, n: 1 },
]

describe('the displayed line', () => {
  it('is null on a day without a value, and never 0', () => {
    const line = smooth(series)
    expect(line[1]).toBeNull()
    expect(line.every((v) => v !== 0)).toBe(true)
    expect(line[2]).toBeGreaterThan(80)
    expect(line[2]).toBeLessThan(82)
  })

  it('only has the confirmed daily values as input', () => {
    const points = chartPoints({ series, band: null })
    expect(points.map((p) => p.value)).toEqual([80, null, 82])
    expect(points[1]?.low).toBeNull()
  })
})

describe('helpers', () => {
  it('clamps the progress to whole percent', () => {
    expect(progressPercent(null)).toBe(0)
    expect(progressPercent({ daysWithData: 1, daysRequired: 2, spanDays: 1, spanRequired: null, fraction: 0.456 })).toBe(45)
    expect(progressPercent({ daysWithData: 1, daysRequired: 2, spanDays: 1, spanRequired: null, fraction: 3 })).toBe(100)
  })

  it('counts a hole between two values, not a leading one', () => {
    expect(longestHole(series)).toBe(1)
    expect(longestHole([{ day: '2026-10-01', value: null, n: 0 }, ...series])).toBe(1)
  })

  it('lists the newest values first', () => {
    expect(recentValues(series, 5).map((p) => p.day)).toEqual(['2026-10-03', '2026-10-01'])
  })
})
