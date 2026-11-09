import { describe, expect, it } from 'vitest'
import type { MetricDetail } from '../api/metrics'
import { DEFAULT_DISPLAY, convertQuantity, formatDayAs, startRange, unitLabel } from './display'
import { convertDetail } from './displayDetail'

// Synthetic numbers only.
const LB = { ...DEFAULT_DISPLAY, weightUnit: 'lb' as const }
const ST = { ...DEFAULT_DISPLAY, weightUnit: 'st' as const }
const F = { ...DEFAULT_DISPLAY, temperatureUnit: 'f' as const }

describe('units', () => {
  it('leaves SI alone by default and converts what is asked', () => {
    expect(convertQuantity(DEFAULT_DISPLAY, 80, 'kg')).toEqual({ value: 80, unit: 'kg' })
    expect(convertQuantity(LB, 80, 'kg').value).toBeCloseTo(176.37, 2)
    expect(convertQuantity(ST, 70, 'kg').value).toBeCloseTo(11.02, 2)
    expect(convertQuantity(F, 36.5, '°C')).toEqual({ value: 97.7, unit: '°F' })
    expect(convertQuantity(LB, 60, 'bpm')).toEqual({ value: 60, unit: 'bpm' })
    expect(unitLabel(ST, 'kg')).toBe('st')
  })

  it('scales a slope and does not offset it', () => {
    const detail = {
      unit: '°C', latest: null, band: null, series: [{ day: '2026-10-01', value: 36, n: 1 }],
      trend: { slopePerDay: 0.1 }, changePoints: { state: 'ready', points: [{ day: '2026-10-01', shift: 1 }] },
    } as unknown as MetricDetail
    const shown = convertDetail(detail, F)
    expect(shown.unit).toBe('°F')
    expect(shown.series[0]?.value).toBeCloseTo(96.8, 5)
    expect(shown.trend.slopePerDay).toBeCloseTo(0.18, 5)
    expect(shown.changePoints.points[0]?.shift).toBeCloseTo(1.8, 5)
    expect(convertDetail(detail, DEFAULT_DISPLAY)).toBe(detail)
  })
})

describe('days', () => {
  it('writes a day in the chosen format', () => {
    expect(formatDayAs('en', 'd_mon_y', '2026-10-14')).toBe('14 Oct 2026')
    expect(formatDayAs('en', 'mon_d_y', '2026-10-14')).toBe('Oct 14, 2026')
    expect(formatDayAs('en', 'iso', '2026-10-14')).toBe('2026-10-14')
    expect(formatDayAs('en', 'iso', '2026-10-14', 'short')).toBe('10-14')
  })
})

describe('the starting range', () => {
  it('uses the saved range when the screen offers it, else the fallback', () => {
    expect(startRange('1y', ['30d', '90d', '1y'], '90d')).toBe('1y')
    expect(startRange('6mo', ['30d', '90d', '1y'], '90d')).toBe('90d')
    expect(startRange(null, ['30d', '90d'], '90d')).toBe('90d')
  })
})
