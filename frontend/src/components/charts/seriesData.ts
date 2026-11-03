// The data of a time-series chart, prepared for uPlot (docs/design/mockups/src/Main.dc.html,
// "The signature chart"). Pure functions: nothing here draws, so the rules the design makes are
// tested on the numbers themselves.
//
// The rules (design principle 2, "Gaps stay gaps"):
//  - a missing value is `null`, never 0: a line breaks at it and nothing is drawn across it;
//  - a run of missing days of `gapDays` or more is a labelled gap ("no data", "9 days"): the chart
//    hatches it and the series carry a `null` inside it, so the smoothed line stops where the
//    data stops;
//  - an unconfirmed reading (the scale could not tell who stepped on it) is shown on its own, as a
//    hollow dot, and is never part of the daily values or the smoothed line.

export const DAY_SECONDS = 86_400

/** A run of missing days this long or longer is a labelled gap (shorter ones are just skipped). */
export const DEFAULT_GAP_DAYS = 7

/** One day of a series. `t` is the start of the day, in epoch seconds. */
export interface ChartPoint {
  t: number
  /** The day's value; `null` when there is none. */
  value: number | null
  /** The smoothed value (gap-weighted EWMA) at this day, when the analysis made one. */
  smoothed?: number | null
  /** The typical range at this day (rolling median ± 1.5 MAD over 90 days). */
  low?: number | null
  high?: number | null
  /** The scale could not tell who stepped on it: shown, never used. */
  unconfirmed?: boolean
}

export interface ChangePoint {
  t: number
  /** "change point · 23 Aug" */
  label: string
}

export interface Gap {
  /** The first and the last missing day, inclusive (epoch seconds). */
  from: number
  to: number
  days: number
}

export interface ChartData {
  /** The x values, in epoch seconds: the days with data, and one marker day inside each gap. */
  x: number[]
  /** The confirmed daily values. */
  value: (number | null)[]
  /** The unconfirmed readings. */
  unconfirmed: (number | null)[]
  smoothed: (number | null)[]
  low: (number | null)[]
  high: (number | null)[]
  gaps: Gap[]
  changePoints: ChangePoint[]
  /** The newest confirmed value. */
  latest: { t: number; value: number } | null
  /** Y bounds that hold every drawn value, with room around them. Never forced to include 0. */
  range: [number, number]
}

export interface BuildOptions {
  gapDays?: number
  changePoints?: ChangePoint[]
}

const finite = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value)

function dayIndex(t: number): number {
  return Math.floor(t / DAY_SECONDS)
}

/** "9 days", "1 day": the wording of a gap's length. */
export function gapLength(days: number): string {
  return days === 1 ? '1 day' : `${String(days)} days`
}

/** The gaps of a series: runs of `gapDays` or more days between two days that have data. */
export function findGaps(daysWithData: number[], gapDays: number = DEFAULT_GAP_DAYS): Gap[] {
  const sorted = [...new Set(daysWithData.map(dayIndex))].sort((a, b) => a - b)
  const gaps: Gap[] = []
  for (let i = 1; i < sorted.length; i += 1) {
    const missing = (sorted[i] ?? 0) - (sorted[i - 1] ?? 0) - 1
    if (missing >= gapDays) {
      gaps.push({
        from: ((sorted[i - 1] ?? 0) + 1) * DAY_SECONDS,
        to: ((sorted[i] ?? 0) - 1) * DAY_SECONDS,
        days: missing,
      })
    }
  }
  return gaps
}

export function buildChartData(points: ChartPoint[], options: BuildOptions = {}): ChartData {
  const gapDays = options.gapDays ?? DEFAULT_GAP_DAYS
  const byDay = new Map<number, ChartPoint[]>()
  for (const point of points) {
    if (!Number.isFinite(point.t)) continue
    const day = dayIndex(point.t)
    byDay.set(day, [...(byDay.get(day) ?? []), point])
  }
  // A day counts as data when it has a confirmed value or a smoothed one; an unconfirmed reading
  // alone does not close a gap ("shown, never used").
  const usable = (point: ChartPoint) =>
    (point.unconfirmed !== true && finite(point.value)) || finite(point.smoothed)
  const usableDays = [...byDay.entries()].filter(([, list]) => list.some(usable)).map(([day]) => day * DAY_SECONDS)
  const gaps = findGaps(usableDays, gapDays)
  const shownDays = [...byDay.entries()]
    .filter(([, list]) => list.some((point) => finite(point.value) || finite(point.smoothed)))
    .map(([day]) => day * DAY_SECONDS)

  // One marker day in the middle of each gap, all series null there: that is what breaks the line.
  const markers = new Map(gaps.map((gap) => [dayIndex(gap.from + Math.floor((gap.days - 1) / 2) * DAY_SECONDS), gap]))
  const days = [...new Set([...shownDays.map(dayIndex), ...markers.keys()])].sort((a, b) => a - b)

  const data: ChartData = {
    x: days.map((day) => day * DAY_SECONDS),
    value: [],
    unconfirmed: [],
    smoothed: [],
    low: [],
    high: [],
    gaps,
    changePoints: options.changePoints ?? [],
    latest: null,
    range: [0, 1],
  }
  const drawn: number[] = []
  const bandCover: number[] = []
  for (const day of days) {
    const list = byDay.get(day) ?? []
    const confirmed = list.find((point) => point.unconfirmed !== true && finite(point.value))
    const unconfirmed = list.find((point) => point.unconfirmed === true && finite(point.value))
    const reference = confirmed ?? list[0]
    const value = confirmed?.value ?? null
    const smoothed = list.map((point) => point.smoothed).find(finite) ?? null
    const low = reference?.low ?? null
    const high = reference?.high ?? null
    const bandOk = finite(low) && finite(high)
    data.value.push(value)
    data.unconfirmed.push(unconfirmed?.value ?? null)
    data.smoothed.push(smoothed)
    data.low.push(bandOk ? low : null)
    data.high.push(bandOk ? high : null)
    for (const v of [value, unconfirmed?.value, smoothed]) if (finite(v)) drawn.push(v)
    if (bandOk) bandCover.push(low, high)
    if (finite(value)) data.latest = { t: day * DAY_SECONDS, value }
  }
  data.range = yRange([...drawn, ...bandCover])
  return data
}

/** Bounds around the drawn values with 8 % of room on each side. No zero is added. */
export function yRange(values: number[]): [number, number] {
  const finiteValues = values.filter(Number.isFinite)
  if (finiteValues.length === 0) return [0, 1]
  const min = Math.min(...finiteValues)
  const max = Math.max(...finiteValues)
  const room = max === min ? Math.max(Math.abs(max) * 0.05, 1) : (max - min) * 0.08
  return [min - room, max + room]
}

/** Whether a value sits outside its typical range (the latest dot turns orange). */
export function isOutside(value: number, low: number | null | undefined, high: number | null | undefined): boolean {
  if (!finite(low) || !finite(high)) return false
  return value < low || value > high
}
