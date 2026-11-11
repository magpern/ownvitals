import { describe, expect, it } from 'vitest'
import { fakeProfile } from '../test/profileFixtures'
import { decodeProfile } from './profile'

// Synthetic data only (see test/profileFixtures.ts).

describe('decodeProfile: chart_range', () => {
  it.each(['7d', '30d', '90d', '6mo', '1y'])('accepts %s', (range) => {
    expect(decodeProfile(fakeProfile({ chart_range: range })).chart_range).toBe(range)
  })

  it.each(['all', '14d', '', null])('refuses %s', (range) => {
    expect(() => decodeProfile(fakeProfile({ chart_range: range }))).toThrow()
  })
})
