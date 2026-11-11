// Pure: the change of the latest reading against the median of the tile's own sparkline days
// (docs/design/motion.md, item 3). No I/O; the caller converts to the saved unit first.

import type { MetricTile } from '../../api/overview'

export function median(values: number[]): number | null {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  const upper = sorted[mid] ?? 0
  return sorted.length % 2 === 1 ? upper : ((sorted[mid - 1] ?? upper) + upper) / 2
}

export interface Delta {
  /** Latest minus median, in the API's unit. */
  amount: number
  direction: 'up' | 'down' | 'flat'
  /** The days the median is taken over (days with a value). */
  days: number
}

/** `null` when there is no latest value or fewer than 3 days to compare with. */
export function deltaOf(tile: MetricTile): Delta | null {
  if (tile.latest === null) return null
  const values = tile.sparkline.flatMap((p) => (p.value === null ? [] : [p.value]))
  const mid = values.length >= 3 ? median(values) : null
  if (mid === null) return null
  const amount = tile.latest.value - mid
  return { amount, direction: Math.abs(amount) < 1e-9 ? 'flat' : amount > 0 ? 'up' : 'down', days: tile.sparkline.length }
}
