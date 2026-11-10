import { describe, expect, it } from 'vitest'
import { belongsToTab, chartRange, chartStart, evidenceParams, inStretch, localDay, locationUrl, readLocation } from './model'

describe('the findings location', () => {
  it('reads the filters and the chosen finding from the address', () => {
    expect(readLocation(new URLSearchParams('tab=history&tier=trend&area=heart&id=x'))).toEqual({
      filters: { tab: 'history', tier: 'trend', area: 'heart' },
      id: 'x',
    })
  })

  it('drops values it does not know and defaults to the live tab', () => {
    expect(readLocation(new URLSearchParams('tab=x&tier=y&area=z')).filters).toEqual({ tab: 'live', tier: null, area: null })
  })

  it('round-trips, leaving defaults out', () => {
    expect(locationUrl({ filters: { tab: 'live', tier: null, area: null }, id: null })).toBe('/findings')
    const loc = { filters: { tab: 'history', tier: 'device', area: 'body' }, id: 'f1' } as const
    expect(readLocation(new URLSearchParams(locationUrl(loc).split('?')[1]))).toEqual(loc)
  })
})

describe('the findings tabs', () => {
  it('keeps backfill findings under history only', () => {
    expect(belongsToTab({ origin: 'backfill' }, 'live')).toBe(false)
    expect(belongsToTab({ origin: 'backfill' }, 'history')).toBe(true)
    expect(belongsToTab({ origin: 'live' }, 'live')).toBe(true)
    expect(belongsToTab({ origin: 'live' }, 'history')).toBe(false)
  })
})

describe('the evidence', () => {
  it('maps evidence keys to the placeholders of the rule text', () => {
    expect(evidenceParams({ gain_kg: 2.1, window_days: 3, median_symmetry_index: 1.4, run_measure_days: 4, name: 'x' })).toEqual({
      gainKg: 2.1,
      windowDays: 3,
      ratio: 1.4,
      runDays: 4,
    })
  })

  it('picks the shortest range that reaches the chart start', () => {
    expect(chartStart('2026-10-29')).toBe('2026-10-01')
    expect(chartRange('2026-10-29', '2026-10-31')).toBe('30d')
    expect(chartRange('2026-09-01', '2026-10-31')).toBe('90d')
    expect(chartRange('2025-01-01', '2026-10-31')).toBe('all')
  })

  it('keeps the days around the window', () => {
    const series = ['2026-09-01', '2026-10-05', '2026-10-30', '2026-11-02'].map((day) => ({ day }))
    expect(inStretch(series, '2026-10-29', '2026-10-30').map((p) => p.day)).toEqual(['2026-10-05', '2026-10-30'])
  })
})

describe('localDay (issue 325)', () => {
  it('uses the local calendar day, not the UTC day', () => {
    expect(localDay(new Date(2026, 10, 1, 0, 30))).toBe('2026-11-01')
    expect(localDay(new Date(2026, 10, 1, 23, 30))).toBe('2026-11-01')
    expect(localDay(new Date(2026, 0, 5, 12))).toBe('2026-01-05')
  })
})
