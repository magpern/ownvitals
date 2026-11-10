import { describe, expect, it } from 'vitest'
import type { Finding } from '../../api/findings'
import type { MetricTile } from '../../api/overview'
import { bodyFindings, missingKeys, segmentTile, segmentValue, symmetryPercent } from './bodyModel'

// Synthetic values only (synthetic: true).
const tile = (key: string, state = 'up_to_date', value: number | null = 3): MetricTile => ({
  key,
  area: 'body',
  unit: 'kg',
  stream: 'measures',
  state,
  progressPercent: null,
  progress: null,
  latest: value === null ? null : { day: '2026-11-01', value, n: 1 },
  typicalRange: null,
  sparkline: [],
})

describe('body model', () => {
  it('finds the tile of a position and reads a value only from a state that has readings', () => {
    const tiles = [tile('segmental_muscle_mass:left_arm'), tile('segmental_muscle_mass:right_arm', 'not_supported')]
    expect(segmentValue(segmentTile(tiles, 'left_arm'))).toBe(3)
    expect(segmentValue(segmentTile(tiles, 'right_arm'))).toBeNull()
    expect(segmentTile(tiles, 'left_leg')).toBeNull()
    expect(segmentValue(null)).toBeNull()
  })

  it('computes the B6 symmetric index and refuses what it cannot', () => {
    expect(symmetryPercent(3.0, 3.0)).toBe(0)
    expect(symmetryPercent(3.3, 2.7)).toBeCloseTo(20, 5)
    expect(symmetryPercent(2.7, 3.3)).toBeCloseTo(20, 5) // left < right: the sign never shows (issue 325)
    expect(symmetryPercent(null, 3)).toBeNull()
    expect(symmetryPercent(0, 0)).toBeNull()
  })

  it('keeps only body rules and lists the metrics the API left out', () => {
    const f = (ruleId: string) => ({ ruleId }) as Finding
    expect(bodyFindings([f('B2'), f('C1'), f('B6')]).map((x) => x.ruleId)).toEqual(['B2', 'B6'])
    expect(missingKeys([tile('weight')])).not.toContain('weight')
    expect(missingKeys([tile('weight')])).toContain('bone_mass')
  })
})
