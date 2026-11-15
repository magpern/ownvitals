import { describe, expect, it } from 'vitest'
import { decodeFinding, decodeFindingsPage, findingPath, findingsListPath } from './findings'
import { LIVE_TREND, page, syntheticFinding } from '../test/findingsFixtures'

describe('the findings client', () => {
  it('decodes a finding and whether it is new', () => {
    const f = decodeFinding(LIVE_TREND)
    expect(f.ruleId).toBe('B1')
    expect(f.isNew).toBe(true)
    expect(f.quality.nDays).toBe(11)
    expect(decodeFinding(syntheticFinding({ seen_at: '2026-11-01T08:00:00Z' })).isNew).toBe(false)
  })

  it('decodes a page and its next offset', () => {
    expect(decodeFindingsPage(page([LIVE_TREND], 20)).nextOffset).toBe(20)
    expect(decodeFindingsPage(page([])).nextOffset).toBeNull()
  })

  it('builds the list and detail paths from the filters', () => {
    expect(findingsListPath({ tab: 'live', tier: null, area: null }, 0)).toBe('/api/findings?tab=live&offset=0')
    expect(findingsListPath({ tab: 'history', tier: 'trend', area: 'heart' }, 20)).toBe('/api/findings?tab=history&tier=trend&area=heart&offset=20')
    expect(findingPath('a/b')).toBe('/api/findings/a%2Fb')
  })
})
