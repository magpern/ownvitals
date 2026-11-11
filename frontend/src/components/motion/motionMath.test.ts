import { describe, expect, it } from 'vitest'
import { countValue, easeOut, staggerIndex } from './motionMath'

describe('motionMath', () => {
  it('easeOut runs 0 to 1, monotonic, front-loaded', () => {
    expect(easeOut(0)).toBe(0)
    expect(easeOut(1)).toBe(1)
    let last = 0
    for (let x = 0.05; x < 1; x += 0.05) {
      const y = easeOut(x)
      expect(y).toBeGreaterThanOrEqual(last)
      last = y
    }
    expect(easeOut(0.5)).toBeGreaterThan(0.5)
  })

  it('countValue ends exactly on the target, up or down, and never overshoots', () => {
    expect(countValue(0, 90, 700, 700)).toBe(90)
    expect(countValue(0, 90, 5000, 700)).toBe(90)
    expect(countValue(0, 90, 0, 0)).toBe(90)
    for (let t = 0; t <= 700; t += 50) {
      expect(countValue(0, 90, t, 700)).toBeLessThanOrEqual(90)
      expect(countValue(90, 10, t, 700)).toBeGreaterThanOrEqual(10)
    }
  })

  it('staggerIndex is a whole non-negative step', () => {
    expect([staggerIndex(-3), staggerIndex(2.7), staggerIndex(NaN), staggerIndex(4)]).toEqual([0, 2, 0, 4])
  })
})
