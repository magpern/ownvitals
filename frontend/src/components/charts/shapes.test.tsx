import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ChartLegend, LEGEND_ORDER } from './ChartLegend'
import { RangeBar, rangeCaption, rangePosition, rangeScale } from './RangeBar'
import { Sparkline, sparkPaths } from './Sparkline'

// Synthetic numbers only.

describe('sparkPaths', () => {
  it('breaks the line at a null: a new "M", never a line through the hole', () => {
    const { line } = sparkPaths([{ value: 79 }, { value: 79.5 }, { value: null }, { value: null }, { value: 78.8 }, { value: 78.5 }])
    expect(line.match(/M/g)).toHaveLength(2)
    expect(line.match(/L/g)).toHaveLength(2)
  })

  it('draws no line for no data, and no dot', () => {
    const empty = sparkPaths([{ value: null }, { value: null }])
    expect(empty.line).toBe('')
    expect(empty.last).toBeNull()
  })

  it('keeps every point inside the box, with no zero baseline', () => {
    const { last } = sparkPaths([{ value: 100, low: 90, high: 110 }, { value: 101, low: 90, high: 110 }])
    expect(last!.y).toBeGreaterThan(0)
    expect(last!.y).toBeLessThan(56)
  })

  it('marks the points outside the typical range, and breaks the band where the range is missing', () => {
    const paths = sparkPaths([
      { value: 70, low: 58, high: 64 },
      { value: 71, low: 58, high: 64 },
      { value: 71, low: null, high: null },
      { value: 60, low: 58, high: 64 },
      { value: 61, low: 58, high: 64 },
    ])
    expect(paths.outside).toHaveLength(2)
    expect(paths.band.match(/Z/g)).toHaveLength(2)
  })
})

describe('Sparkline', () => {
  it('is decoration without a label, and an image with one', () => {
    const { container, rerender } = render(<Sparkline points={[{ value: 1 }, { value: 2 }]} />)
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    rerender(<Sparkline points={[{ value: 1 }, { value: 2 }]} label="Weight, last 90 days" />)
    expect(screen.getByRole('img', { name: 'Weight, last 90 days' })).toBeInTheDocument()
  })

  it('turns the latest dot orange when told the value is outside', () => {
    const { container } = render(<Sparkline points={[{ value: 1 }, { value: 2 }]} attention />)
    expect(container.querySelector('.ov-c-last')).toHaveClass('ov-c-last--attn')
  })
})

describe('RangeBar', () => {
  it('words the position as the design does', () => {
    expect(rangeCaption(rangePosition(78.7, 78.3, 79.5), '78.3–79.5')).toBe('Within your typical range 78.3–79.5')
    expect(rangeCaption(rangePosition(71, 59, 63), '59–63')).toBe('Above your typical range 59–63')
    expect(rangePosition(50, 59, 63)).toBe('below')
  })

  it('puts the value inside the bar and the band inside the bar, whatever the value', () => {
    for (const value of [40, 61, 90]) {
      const scale = rangeScale(value, 59, 63)
      expect(scale.value).toBeGreaterThanOrEqual(0)
      expect(scale.value).toBeLessThanOrEqual(100)
      expect(scale.band[0]).toBeGreaterThanOrEqual(0)
      expect(scale.band[0] + scale.band[1]).toBeLessThanOrEqual(100)
    }
  })

  it('is an image that says where today sits; orange (not red) outside', () => {
    const { container, rerender } = render(<RangeBar value={78.7} low={78.3} high={79.5} />)
    expect(screen.getByRole('img', { name: 'Within your typical range 78.3–79.5' })).toBeInTheDocument()
    expect(container.querySelector('circle')).not.toHaveClass('ov-c-last--attn')
    rerender(<RangeBar value={71} low={59} high={63} />)
    expect(container.querySelector('circle')).toHaveClass('ov-c-last--attn')
    expect(screen.getAllByText('Above your typical range 59–63').length).toBeGreaterThan(0)
  })
})

describe('ChartLegend', () => {
  it('has the seven entries of the design, in its order and words', () => {
    const { container } = render(<ChartLegend />)
    expect(container.querySelectorAll('li')).toHaveLength(LEGEND_ORDER.length)
    expect(container.textContent).toContain('Gap-weighted EWMA. It stops where the data stops.')
    expect(container.textContent).toContain('Orange when outside your typical range, never red.')
  })

  it('leaves out what a series lacks', () => {
    const { container } = render(<ChartLegend items={['band', 'daily']} />)
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })
})
