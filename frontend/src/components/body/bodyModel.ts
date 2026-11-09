import type { Finding } from '../../api/findings'
import type { MetricTile } from '../../api/overview'

// What the body composition page decides, with no I/O (docs/design/README.md, "Body composition"):
// which tiles it shows, which tiles are the segmental rows, the left/right difference as a plain
// number and which findings belong on it. Text and the clock are not here.

/** The tiles of the page, in order; each is the overview's tile. */
export const BODY_KEYS = [
  'weight',
  'fat_ratio',
  'fat_free_mass',
  'fat_mass',
  'muscle_mass',
  'hydration',
  'bone_mass',
  'extracellular_water',
  'intracellular_water',
  'visceral_fat',
  'metabolic_age',
] as const

export const SEGMENT_KEY = 'segmental_muscle_mass'
export const POSITIONS = ['left_arm', 'right_arm', 'left_leg', 'right_leg'] as const
export type Position = (typeof POSITIONS)[number]

/** The rules of the body area. B1 and B3 are experimental: the API sends them only with the owner flag. */
const RULES = ['B1', 'B2', 'B3', 'B4', 'B5', 'B6']

/** The tile of a body position, or `null` when the API lists none. */
export function segmentTile(tiles: readonly MetricTile[], position: Position): MetricTile | null {
  return tiles.find((t) => t.key === `${SEGMENT_KEY}:${position}`) ?? null
}

/** A tile's latest value when its state may show one. */
export function segmentValue(tile: MetricTile | null): number | null {
  if (tile === null || tile.latest === null) return null
  return ['up_to_date', 'stale', 'incomplete'].includes(tile.state) ? tile.latest.value : null
}

/** `|L - R| / ((L + R) / 2)` as the B6 rule defines it, as a percentage; `null` when it cannot be told. */
export function symmetryPercent(left: number | null, right: number | null): number | null {
  if (left === null || right === null || left + right <= 0) return null
  return (Math.abs(left - right) / ((left + right) / 2)) * 100
}

/** The findings of the page: body rules only. Experimental ones are there only if the API sent them. */
export function bodyFindings(findings: readonly Finding[]): Finding[] {
  return findings.filter((f) => RULES.includes(f.ruleId))
}

/** The body tiles the API did not list at all (they read "not available", never nothing). */
export function missingKeys(tiles: readonly MetricTile[]): string[] {
  return BODY_KEYS.filter((k) => !tiles.some((t) => t.key === k))
}
