import type { HisBand, HisCriterion } from '../../api/scoreDetail'

// The rules of the Health score panel (issue 493): which band word a value falls in and how a
// criterion's boundaries read. Pure. The points themselves come from the server; the word is
// derived from the same boundaries (backend/src/ownvitals/analysis/scores/his.py): inside the
// perfect range is Perfect, up to the "good" boundary Good, up to "average" Average, beyond Bad.

export type BandWord = 'perfect' | 'good' | 'average' | 'bad'

/** The side of the perfect range the value is on, or `null` inside it. */
function sideOf(value: number, low: HisBand | null, high: HisBand | null): HisBand | null {
  if (low !== null && value < low.perfect) return low
  if (high !== null && value > high.perfect) return high
  return null
}

/** The word of a value, or `null` when the criterion's boundaries were not sent. */
export function bandWord(value: number, low: HisBand | null, high: HisBand | null): BandWord | null {
  if (low === null && high === null) return null
  const side = sideOf(value, low, high)
  if (side === null) return 'perfect'
  // Distance past the perfect boundary, in the direction away from the perfect range.
  const sign = side.bad <= side.perfect ? -1 : 1
  const past = (boundary: number) => (value - boundary) * sign
  if (past(side.good) <= 0) return 'good'
  return past(side.average) <= 0 ? 'average' : 'bad'
}

export function num(value: number): string {
  return String(Math.round(value * 100) / 100)
}

/** The perfect range, "18.5–25", "≥ 9000" or "≤ 5". */
export function perfectRange(low: HisBand | null, high: HisBand | null): string {
  if (low !== null && high !== null) return `${num(low.perfect)}–${num(high.perfect)}`
  return low !== null ? `≥ ${num(low.perfect)}` : `≤ ${num((high as HisBand).perfect)}`
}

/** A level's boundary on each side that has one, ascending: "17–27", or "7000" on one side. */
export function levelBounds(level: 'good' | 'average' | 'bad', low: HisBand | null, high: HisBand | null): string {
  return [low?.[level], high?.[level]]
    .filter((v): v is number => v !== undefined)
    .sort((a, b) => a - b)
    .map(num)
    .join('–')
}

export function hasBands(c: HisCriterion): boolean {
  return c.low !== null || c.high !== null
}

/** The Bad line's bounds: Bad begins beyond the Average boundary, as `bandWord` decides. */
export function badBeyond(low: HisBand | null, high: HisBand | null): { below: number | null; above: number | null } {
  return { below: low?.average ?? null, above: high?.average ?? null }
}

/** The id of the card's "How it's computed" link: the panel gives the focus back to it. */
export const SCORE_LINK_ID = 'ov-score-link'
