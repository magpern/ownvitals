import { describe, expect, it } from 'vitest'
import { DAY_SECONDS, buildChartData, findGaps, gapLength, isOutside, yRange } from './seriesData'
import { SYNTHETIC_START, syntheticWeight } from './synthetic'

// The rules of the chart grammar, on the numbers: gaps stay gaps (design principle 2).

describe('buildChartData', () => {
  const data = buildChartData(syntheticWeight(), { changePoints: [{ t: SYNTHETIC_START + 45 * DAY_SECONDS, label: 'change point · 15 Feb' }] })

  it('finds a 9-day gap, and says how long it is', () => {
    expect(data.gaps).toEqual([
      { from: SYNTHETIC_START + 20 * DAY_SECONDS, to: SYNTHETIC_START + 28 * DAY_SECONDS, days: 9 },
    ])
    expect(gapLength(9)).toBe('9 days')
    expect(gapLength(1)).toBe('1 day')
  })

  it('breaks every series inside the gap: null there, never 0 and never an interpolated value', () => {
    const inside = data.x.map((t, i) => [t, i] as const).filter(([t]) => t >= data.gaps[0]!.from && t <= data.gaps[0]!.to)
    expect(inside).toHaveLength(1) // one marker day, in the middle of the gap
    for (const [, i] of inside) {
      expect(data.value[i]).toBeNull()
      expect(data.smoothed[i]).toBeNull()
      expect(data.low[i]).toBeNull()
      expect(data.high[i]).toBeNull()
    }
    expect(data.value).not.toContain(0)
    expect(data.smoothed).not.toContain(0)
  })

  it('keeps the series aligned with x, in order', () => {
    for (const series of [data.value, data.unconfirmed, data.smoothed, data.low, data.high]) expect(series).toHaveLength(data.x.length)
    expect([...data.x].sort((a, b) => a - b)).toEqual(data.x)
  })

  it('does not make a gap of two missing days: they are skipped', () => {
    expect(data.gaps).toHaveLength(1)
    expect(data.x).not.toContain(SYNTHETIC_START + 40 * DAY_SECONDS)
  })

  it('keeps unconfirmed readings out of the values and the range of the line', () => {
    expect(data.unconfirmed.filter((v) => v !== null)).toEqual([82.5, 84])
    expect(data.value).not.toContain(82.5)
    expect(data.value).not.toContain(84)
  })

  it('has the newest confirmed value as the latest', () => {
    expect(data.latest?.t).toBe(SYNTHETIC_START + 59 * DAY_SECONDS)
  })

  it('bounds the y axis around the data, without a zero', () => {
    const [min, max] = data.range
    expect(min).toBeGreaterThan(70)
    expect(max).toBeGreaterThan(84)
  })

  it('a day with only an unconfirmed reading does not close a gap', () => {
    const points = [
      { t: SYNTHETIC_START, value: 80 },
      { t: SYNTHETIC_START + 5 * DAY_SECONDS, value: 90, unconfirmed: true },
      { t: SYNTHETIC_START + 10 * DAY_SECONDS, value: 80 },
    ]
    expect(buildChartData(points).gaps).toHaveLength(1)
  })

  it('is empty for no data', () => {
    const empty = buildChartData([])
    expect(empty.x).toEqual([])
    expect(empty.latest).toBeNull()
    expect(empty.range).toEqual([0, 1])
  })
})

describe('findGaps', () => {
  it('uses the length given', () => {
    const days = [0, 3].map((d) => d * DAY_SECONDS)
    expect(findGaps(days, 2)).toHaveLength(1)
    expect(findGaps(days, 3)).toHaveLength(0)
  })
})

describe('yRange and isOutside', () => {
  it('adds room and never a zero', () => {
    const [min, max] = yRange([78, 80])
    expect(min).toBeCloseTo(77.84)
    expect(max).toBeCloseTo(80.16)
    expect(yRange([5, 5])[0]).toBeGreaterThan(0)
  })

  it('knows when a value is outside its range, and says no when the range is missing', () => {
    expect(isOutside(81, 78, 80)).toBe(true)
    expect(isOutside(77, 78, 80)).toBe(true)
    expect(isOutside(79, 78, 80)).toBe(false)
    expect(isOutside(79, null, 80)).toBe(false)
  })
})
