import { DAY_SECONDS, type ChartPoint } from './seriesData'

// Synthetic series for the tests of the chart components: made-up numbers, no real health data.
// `synthetic_weight` is 60 days of "weight" with a 9-day hole, two unconfirmed readings and a short
// run of missing days that is too short to be a gap.

export const SYNTHETIC_START = Date.UTC(2026, 0, 1) / 1000

export function syntheticWeight(): ChartPoint[] {
  const points: ChartPoint[] = []
  for (let day = 0; day < 60; day += 1) {
    const t = SYNTHETIC_START + day * DAY_SECONDS
    if (day >= 20 && day <= 28) continue // the 9-day gap
    if (day === 40 || day === 41) continue // two missing days: not a gap
    const value = 79.5 - day * 0.01 + (day % 3) * 0.1
    points.push({ t, value, smoothed: 79.4 - day * 0.01, low: 78.4, high: 80.4 })
  }
  points.push({ t: SYNTHETIC_START + 10 * DAY_SECONDS, value: 82.5, unconfirmed: true })
  points.push({ t: SYNTHETIC_START + 50 * DAY_SECONDS, value: 84, unconfirmed: true })
  return points
}
