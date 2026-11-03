import { DAY_SECONDS, gapLength, isOutside, type ChartData } from './seriesData'
import type { ChartColours } from './tokens'

// What uPlot does not draw itself: the typical-range band, the hatched and labelled gaps, the daily
// dots, the unconfirmed readings, the change points and the latest value (the legend of
// docs/design/mockups/src/Main.dc.html). They take a plain 2-D context and a projection, so a test
// can run them against a recording context. Sizes are in CSS pixels times `ratio`.

/** The part of CanvasRenderingContext2D the painters use. */
export interface Paint {
  fillStyle: string | CanvasPattern
  strokeStyle: string
  lineWidth: number
  globalAlpha: number
  font: string
  textAlign: CanvasTextAlign
  textBaseline: CanvasTextBaseline
  save(): void
  restore(): void
  beginPath(): void
  moveTo(x: number, y: number): void
  lineTo(x: number, y: number): void
  closePath(): void
  arc(x: number, y: number, r: number, from: number, to: number): void
  fill(): void
  stroke(): void
  fillRect(x: number, y: number, w: number, h: number): void
  rect(x: number, y: number, w: number, h: number): void
  clip(): void
  setLineDash(segments: number[]): void
  measureText(text: string): { width: number }
  fillText(text: string, x: number, y: number): void
}

export interface Projection {
  /** Epoch seconds, then value, to canvas pixels. */
  x(t: number): number
  y(v: number): number
  /** The plot area in canvas pixels. */
  left: number
  top: number
  width: number
  height: number
  /** Canvas pixels per CSS pixel. */
  ratio: number
}

export interface Labels {
  unit?: string
  /** How the latest value is written: "78.7 kg". */
  format?: (value: number) => string
}

function circle(ctx: Paint, x: number, y: number, r: number): void {
  ctx.beginPath()
  ctx.arc(x, y, r, 0, Math.PI * 2)
}

function clipToPlot(ctx: Paint, p: Projection): void {
  ctx.beginPath()
  ctx.rect(p.left, p.top, p.width, p.height)
  ctx.clip()
}

/** Under the series: the gaps (ground colour, then hatched) and the typical-range band. */
export function paintBack(ctx: Paint, p: Projection, data: ChartData, colours: ChartColours, hatch: string | CanvasPattern): void {
  ctx.save()
  clipToPlot(ctx, p)
  for (const gap of data.gaps) {
    const x0 = Math.max(p.left, p.x(gap.from - DAY_SECONDS / 2))
    const x1 = Math.min(p.left + p.width, p.x(gap.to + DAY_SECONDS / 2))
    ctx.fillStyle = colours.ground
    ctx.fillRect(x0, p.top, x1 - x0, p.height)
    ctx.fillStyle = hatch
    ctx.fillRect(x0, p.top, x1 - x0, p.height)
  }

  // The band is drawn in runs: it breaks where the range is missing, as the line does.
  ctx.fillStyle = colours.band
  let run: number[] = []
  const flush = () => {
    if (run.length > 1) {
      ctx.beginPath()
      run.forEach((i, n) => {
        const fn = n === 0 ? ctx.moveTo.bind(ctx) : ctx.lineTo.bind(ctx)
        fn(p.x(data.x[i] ?? 0), p.y(data.high[i] ?? 0))
      })
      for (let n = run.length - 1; n >= 0; n -= 1) {
        const i = run[n] ?? 0
        ctx.lineTo(p.x(data.x[i] ?? 0), p.y(data.low[i] ?? 0))
      }
      ctx.closePath()
      ctx.fill()
    }
    run = []
  }
  data.x.forEach((_, i) => {
    if (data.low[i] === null || data.high[i] === null) flush()
    else run.push(i)
  })
  flush()
  ctx.restore()
}

/** Over the series: dots, unconfirmed readings, gap labels, change points and the latest value. */
export function paintFront(ctx: Paint, p: Projection, data: ChartData, colours: ChartColours, labels: Labels = {}): void {
  const r = p.ratio
  const font = (size: number, weight = 400) => `${String(weight)} ${String(size * r)}px ${colours.font || 'sans-serif'}`
  ctx.save()
  clipToPlot(ctx, p)

  // Daily values: small, half transparent, so the smoothed line reads first.
  ctx.fillStyle = colours.series
  ctx.globalAlpha = 0.5
  data.x.forEach((t, i) => {
    const v = data.value[i]
    if (v === null || v === undefined) return
    circle(ctx, p.x(t), p.y(v), 2.5 * r)
    ctx.fill()
  })
  ctx.globalAlpha = 1

  // Unconfirmed: hollow, never part of the line.
  ctx.fillStyle = colours.surface
  ctx.strokeStyle = colours.axis
  ctx.lineWidth = 1.5 * r
  let lastUnconfirmed: [number, number] | null = null
  data.x.forEach((t, i) => {
    const v = data.unconfirmed[i]
    if (v === null || v === undefined) return
    circle(ctx, p.x(t), p.y(v), 4 * r)
    ctx.fill()
    ctx.stroke()
    lastUnconfirmed = [p.x(t), p.y(v)]
  })

  ctx.fillStyle = colours.axis
  ctx.textBaseline = 'alphabetic'
  ctx.font = font(11)
  ctx.textAlign = 'center'
  for (const gap of data.gaps) {
    const mid = (p.x(gap.from - DAY_SECONDS / 2) + p.x(gap.to + DAY_SECONDS / 2)) / 2
    ctx.fillText('no data', mid, p.top + 14 * r)
    ctx.fillText(gapLength(gap.days), mid, p.top + 28 * r)
  }
  if (lastUnconfirmed !== null) {
    const [x, y] = lastUnconfirmed as [number, number]
    // The note goes on the side with room: a reading near the right edge gets it on its left.
    const room = x + 8 * r + ctx.measureText('unconfirmed').width <= p.left + p.width
    ctx.textAlign = room ? 'left' : 'right'
    ctx.fillText('unconfirmed', room ? x + 8 * r : x - 8 * r, y + 4 * r)
  }

  // Change points: a dashed line and its note, only where the analysis found one.
  ctx.strokeStyle = colours.text
  ctx.lineWidth = r
  for (const cp of data.changePoints) {
    const x = p.x(cp.t)
    ctx.setLineDash([4 * r, 4 * r])
    ctx.beginPath()
    ctx.moveTo(x, p.top)
    ctx.lineTo(x, p.top + p.height)
    ctx.stroke()
    ctx.setLineDash([])
    ctx.fillStyle = colours.axis
    ctx.textAlign = 'left'
    ctx.fillText(cp.label, x + 8 * r, p.top + p.height - 8 * r)
  }

  ctx.restore()

  // Latest: bigger and labelled; orange when outside the typical range, never red.
  // Not clipped: the dot and its label sit at the right edge of the plot.
  ctx.save()
  const latest = data.latest
  if (latest !== null) {
    const i = data.x.indexOf(latest.t)
    const outside = isOutside(latest.value, data.low[i], data.high[i])
    const x = p.x(latest.t)
    const y = p.y(latest.value)
    ctx.fillStyle = outside ? colours.attn : colours.series
    ctx.strokeStyle = colours.surface
    ctx.lineWidth = 2.5 * r
    circle(ctx, x, y, 5.5 * r)
    ctx.fill()
    ctx.stroke()
    const text = labels.format ? labels.format(latest.value) : String(latest.value)
    ctx.fillStyle = outside ? colours.attn : colours.text
    ctx.font = font(12, 500)
    ctx.textAlign = 'right'
    ctx.fillText(labels.unit ? `${text} ${labels.unit}` : text, x + 2 * r, y - 12 * r)
  }
  ctx.restore()

  // The unit sits above the y axis, outside the plot area.
  if (labels.unit) {
    ctx.fillStyle = colours.axis
    ctx.font = font(11)
    ctx.textAlign = 'right'
    ctx.fillText(labels.unit, p.left - 8 * r, p.top - 8 * r)
  }
}
