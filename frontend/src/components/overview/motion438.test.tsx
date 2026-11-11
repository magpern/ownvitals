import { render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MetricTile } from '../../api/overview'
import { Sparkline } from '../charts/Sparkline'
import { useNewReading } from '../motion'
import { fakeMatchMedia } from '../motion/motionFakes'
import { DeltaChip } from './DeltaChip'
import { deltaOf, median } from './deltaModel'

// Synthetic values only (labelled: synthetic: true).
const base: MetricTile = {
  key: 'weight', area: 'body', unit: 'kg', stream: 'measure', state: 'up_to_date',
  progressPercent: null, progress: null, typicalRange: null,
  latest: { day: '2026-01-10', value: 82, n: 1 },
  sparkline: [80, 80, 81, null, 80].map((value, i) => ({ day: `2026-01-0${String(i + 1)}`, value })),
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('deltaModel', () => {
  it('takes the median of even and odd lists', () => {
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
    expect(median([])).toBeNull()
  })

  it('compares the latest value with the median of the days with a value', () => {
    expect(deltaOf(base)).toMatchObject({ amount: 2, direction: 'up', days: 5 })
  })

  it('gives no delta without a latest value or with under 3 days', () => {
    expect(deltaOf({ ...base, latest: null })).toBeNull()
    expect(deltaOf({ ...base, sparkline: base.sparkline.slice(0, 2) })).toBeNull()
  })
})

describe('DeltaChip', () => {
  it('shows a signed change and what it is compared with, with a hidden arrow', () => {
    const { container } = render(<DeltaChip tile={base} digits={1} attention={false} />)
    expect(screen.getByText(/\+2\.0 vs your 5-day median/)).toBeInTheDocument()
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull()
    expect(container.firstChild).not.toHaveClass('ov-delta--attn')
  })

  it('uses the attention colour class outside the range and renders nothing without data', () => {
    const { container, rerender } = render(<DeltaChip tile={base} digits={1} attention />)
    expect(container.firstChild).toHaveClass('ov-delta--attn')
    rerender(<DeltaChip tile={{ ...base, latest: null }} digits={1} attention={false} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('Sparkline pulse', () => {
  const points = [{ value: 1 }, { value: 2 }]
  it('draws the halo only when asked', () => {
    const { container, rerender } = render(<Sparkline points={points} />)
    expect(container.querySelector('.ov-c-pulse')).toBeNull()
    rerender(<Sparkline points={points} pulse />)
    expect(container.querySelector('.ov-c-pulse')).not.toBeNull()
  })
})

describe('useNewReading', () => {
  it('is false on first mount, true when a new reading arrives', () => {
    fakeMatchMedia(false)
    const { result, rerender } = renderHook(({ id }) => useNewReading(id), { initialProps: { id: '2026-01-09' } })
    expect(result.current).toBe(false)
    rerender({ id: '2026-01-10' })
    expect(result.current).toBe(true)
    rerender({ id: '2026-01-10' })
    expect(result.current).toBe(true) // still that reading; the one-shot CSS animation does not replay
  })

  it('stays false under reduced motion and while the tab is hidden', () => {
    fakeMatchMedia(true)
    const reduced = renderHook(({ id }) => useNewReading(id), { initialProps: { id: 'a' } })
    reduced.rerender({ id: 'b' })
    expect(reduced.result.current).toBe(false)
    fakeMatchMedia(false)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const hidden = renderHook(({ id }) => useNewReading(id), { initialProps: { id: 'a' } })
    hidden.rerender({ id: 'b' })
    expect(hidden.result.current).toBe(false)
  })
})
