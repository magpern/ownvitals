import type { MetricDetail, Progress, SeriesPoint } from '../../api/metrics'
import { DAY_SECONDS, type ChartPoint } from '../charts/seriesData'

// The numbers of the metric screens, prepared for drawing. Pure: no I/O, no clock, no language.
//
// The API sends the confirmed daily values and a typical range; the smoothed line is a display
// choice made here (the gap-weighted EWMA of the design, see the legend): it weights a value by
// the days since the one before, so it stops where the data stops, and a day without a value is
// `null` and never 0. An unconfirmed reading is never an input of any of it.

/** The smoothing of the displayed line (per day), as in the design's chart. */
export const SMOOTHING_ALPHA = 0.25

/** The start of a day (the API's date, as it is), in epoch seconds. */
export function daySeconds(day: string): number {
  const [year, month, date] = day.split('-').map(Number)
  return Date.UTC(year ?? 1970, (month ?? 1) - 1, date ?? 1) / 1000
}

/** The gap-weighted EWMA of the days that have a value; `null` where there is none. */
export function smooth(series: readonly SeriesPoint[], alpha = SMOOTHING_ALPHA): (number | null)[] {
  let current: number | null = null
  let previous = 0
  return series.map((point) => {
    if (point.value === null) return null
    const t = daySeconds(point.day) / DAY_SECONDS
    current = current === null ? point.value : current + (1 - (1 - alpha) ** (t - previous)) * (point.value - current)
    previous = t
    return current
  })
}

/** The points of the chart: confirmed values only, the typical range on every day that has one. */
export function chartPoints(detail: Pick<MetricDetail, 'series' | 'band'>): ChartPoint[] {
  const smoothed = smooth(detail.series)
  return detail.series.map((point, i) => ({
    t: daySeconds(point.day),
    value: point.value,
    smoothed: smoothed[i] ?? null,
    low: point.value === null ? null : (detail.band?.low ?? null),
    high: point.value === null ? null : (detail.band?.high ?? null),
  }))
}

/** 0 to 100, whole numbers, of the least advanced part of the minimum-data requirement. */
export function progressPercent(progress: Progress | null): number {
  if (progress === null || !Number.isFinite(progress.fraction)) return 0
  return Math.min(100, Math.max(0, Math.floor(progress.fraction * 100)))
}

/** The newest `count` days that have a value, newest first. */
export function recentValues(series: readonly SeriesPoint[], count: number): (SeriesPoint & { value: number })[] {
  return series
    .filter((point): point is SeriesPoint & { value: number } => point.value !== null)
    .slice(-count)
    .reverse()
}

/** Days of the series that have a value. */
export function daysWithValue(series: readonly SeriesPoint[]): number {
  return series.filter((point) => point.value !== null).length
}

/** The longest run of days without a value between two days that have one (a leading hole is not a gap). */
export function longestHole(series: readonly SeriesPoint[]): number {
  let longest = 0
  let run = 0
  let seen = false
  for (const point of series) {
    if (point.value === null) {
      if (seen) run += 1
    } else {
      longest = Math.max(longest, run)
      run = 0
      seen = true
    }
  }
  return longest
}

/** `kg a month` from a slope per day: the design shows the trend over 30 days. */
export function slopePerMonth(slopePerDay: number | null): number | null {
  return slopePerDay === null ? null : slopePerDay * 30
}

/** The decimals of an exact decimal string ("78.35" is 2): a reading is shown with the digits it was stored with. */
export function decimalsOf(exact: string): number {
  const dot = exact.indexOf('.')
  return dot < 0 ? 0 : exact.length - dot - 1
}
