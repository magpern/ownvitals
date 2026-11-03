import { useEffect, useRef } from 'react'
import uPlot from 'uplot'
import { paintBack, paintFront, type Labels, type Paint, type Projection } from './painters'
import type { ChartData } from './seriesData'
import { readChartColours, useThemeVersion, type ChartColours } from './tokens'

// The signature chart of the design (docs/design/mockups/src/Main.dc.html): one time series on its
// own typical range. uPlot (MIT) does the scales, the axes and the smoothed line; `painters.ts`
// draws the rest on the same canvas. A missing value is `null` and uPlot is told not to span it:
// a line breaks, and nothing is ever drawn across a gap or through zero.

const GAP_HATCH_PX = 6

/** The inputs of the uPlot options (also what a test passes to look at them). */
export interface ChartSpec extends Labels {
  data: ChartData
  colours: ChartColours
  width: number
  height: number
}

/** The arrays uPlot is given: x, smoothed line, daily values, unconfirmed readings. */
export function uplotData(data: ChartData): uPlot.AlignedData {
  return [data.x, data.smoothed, data.value, data.unconfirmed]
}

function hatchPattern(ctx: CanvasRenderingContext2D, colours: ChartColours, ratio: number): CanvasPattern | string {
  const size = Math.round(GAP_HATCH_PX * ratio)
  const tile = document.createElement('canvas')
  tile.width = size
  tile.height = size
  const paint = tile.getContext('2d')
  if (paint === null) return colours.line
  paint.strokeStyle = colours.line
  paint.lineWidth = 2 * ratio
  paint.beginPath()
  // A 45 degree line that meets itself across the tile edges.
  for (const offset of [-size, 0, size]) {
    paint.moveTo(offset, size)
    paint.lineTo(offset + size, 0)
  }
  paint.stroke()
  return ctx.createPattern(tile, 'repeat') ?? colours.line
}

function projection(u: uPlot): Projection {
  return {
    x: (t) => u.valToPos(t, 'x', true),
    y: (v) => u.valToPos(v, 'y', true),
    left: u.bbox.left,
    top: u.bbox.top,
    width: u.bbox.width,
    height: u.bbox.height,
    ratio: uPlot.pxRatio,
  }
}

export function chartOptions({ data, colours, width, height, unit, format }: ChartSpec): uPlot.Options {
  const font = `11px ${colours.font || 'sans-serif'}`
  const axis = {
    stroke: colours.axis,
    font,
    grid: { stroke: colours.grid, width: 1 },
    ticks: { show: false },
    border: { show: false },
  }
  const xAxis = {
    ...axis,
    // The mock-up has horizontal rules only.
    grid: { show: false },
    values: (_u: uPlot, splits: number[], _axis: number, _space: number, incr: number) =>
      splits.map((t) =>
        new Date(t * 1000).toLocaleDateString('en-GB', {
          timeZone: 'UTC',
          month: 'short',
          ...(incr >= 28 * 86_400 ? {} : { day: 'numeric' as const }),
        }),
      ),
  }
  const labels: Labels = { unit, format }
  return {
    width,
    height,
    padding: [20, 28, 0, 0],
    legend: { show: false },
    cursor: { show: false },
    select: { show: false, left: 0, top: 0, width: 0, height: 0 },
    scales: {
      x: { time: true },
      // The bounds come from the data, with room around: the axis never starts at 0 by itself.
      y: { range: () => data.range },
    },
    axes: [
      { ...xAxis, size: 28 },
      { ...axis, size: 44 },
    ],
    series: [
      {},
      { label: 'Smoothed', stroke: colours.series, width: 2, spanGaps: false, points: { show: false } },
      // Painted by painters.ts, so uPlot draws neither a path nor points for these two.
      { label: 'Daily value', paths: () => null, points: { show: false } },
      { label: 'Unconfirmed', paths: () => null, points: { show: false } },
    ],
    hooks: {
      drawClear: [
        (u) => {
          paintBack(u.ctx as unknown as Paint, projection(u), data, colours, hatchPattern(u.ctx, colours, uPlot.pxRatio))
        },
      ],
      draw: [
        (u) => {
          paintFront(u.ctx as unknown as Paint, projection(u), data, colours, labels)
        },
      ],
    },
  }
}

interface Props extends Labels {
  data: ChartData
  /** What the chart shows, for a screen reader: "Weight over 6 months, 180 days, 9 days missing". */
  label: string
  height?: number
}

export function TimeSeriesChart({ data, label, unit, format, height = 300 }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const theme = useThemeVersion()

  // The chart is made again when the data or the theme changes, and follows the width of its box.
  useEffect(() => {
    const el = host.current
    if (el === null) return
    const widthNow = () => Math.max(Math.floor(el.getBoundingClientRect().width), 1)
    const u = new uPlot(
      chartOptions({ data, colours: readChartColours(), width: widthNow(), height, unit, format }),
      uplotData(data),
      el,
    )
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      u.setSize({ width: widthNow(), height })
    })
    observer?.observe(el)
    return () => {
      observer?.disconnect()
      u.destroy()
    }
    // `theme` is not read here: a new value is what makes the colours be read again.
  }, [data, unit, format, height, theme])

  return <div className="ov-chart" ref={host} role="img" aria-label={label} data-testid="time-series-chart" />
}
