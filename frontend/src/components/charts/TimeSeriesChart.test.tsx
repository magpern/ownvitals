import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type uPlot from 'uplot'
import { buildChartData } from './seriesData'
import { syntheticWeight } from './synthetic'
import { TimeSeriesChart, chartOptions, uplotData } from './TimeSeriesChart'
import { readChartColours } from './tokens'

// uPlot needs a canvas, which jsdom has not: it is replaced by a recorder, and the test looks at
// what the chart gives it (the data, the options). The drawing is tested in painters.test.ts.

const made: { options: uPlot.Options; data: uPlot.AlignedData; destroyed: boolean; sizes: unknown[] }[] = []

vi.mock('uplot', () => {
  class FakePlot {
    static pxRatio = 1
    record: (typeof made)[number]
    constructor(options: uPlot.Options, data: uPlot.AlignedData) {
      this.record = { options, data, destroyed: false, sizes: [] }
      made.push(this.record)
    }
    destroy() {
      this.record.destroyed = true
    }
    setSize(size: unknown) {
      this.record.sizes.push(size)
    }
  }
  return { default: FakePlot }
})

beforeEach(() => {
  made.length = 0
})

describe('the data given to uPlot', () => {
  const chart = buildChartData(syntheticWeight())

  it('has nulls across the 9-day gap, and no zero anywhere', () => {
    const [x, smoothed, value] = uplotData(chart)
    expect(x).toBe(chart.x)
    for (const series of [smoothed, value]) {
      expect(series).toContain(null)
      expect(series).not.toContain(0)
    }
    const gap = chart.gaps[0]!
    const marker = chart.x.findIndex((t) => t >= gap.from && t <= gap.to)
    expect(smoothed?.[marker]).toBeNull()
    expect(value?.[marker]).toBeNull()
  })

  it('tells uPlot not to join across a null, and keeps zero out of the y scale', () => {
    const options = chartOptions({ data: chart, colours: readChartColours(), width: 600, height: 300 })
    const line = options.series[1]!
    expect(line.spanGaps).toBe(false)
    const range = options.scales?.y?.range as () => [number, number]
    const [min] = range()
    expect(min).toBeGreaterThan(70)
    // The legend and cursor are the app's own: uPlot's are off.
    expect(options.legend?.show).toBe(false)
    expect(options.cursor?.show).toBe(false)
  })
})

describe('TimeSeriesChart', () => {
  it('is an image with its label, makes the chart with the data, and tears it down', () => {
    const data = buildChartData(syntheticWeight())
    const { getByRole, unmount } = render(<TimeSeriesChart data={data} label="Weight, 60 days, 9 days missing" unit="kg" />)
    expect(getByRole('img', { name: 'Weight, 60 days, 9 days missing' })).toBeInTheDocument()
    expect(made).toHaveLength(1)
    expect(made[0]?.data[1]).toBe(data.smoothed)
    unmount()
    expect(made[0]?.destroyed).toBe(true)
  })

  it('makes the chart again for new data', () => {
    const { rerender } = render(<TimeSeriesChart data={buildChartData(syntheticWeight())} label="a" />)
    rerender(<TimeSeriesChart data={buildChartData(syntheticWeight().slice(0, 20))} label="a" />)
    expect(made).toHaveLength(2)
    expect(made[0]?.destroyed).toBe(true)
  })
})
