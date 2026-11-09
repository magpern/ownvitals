import { MM_PER_SECOND, PULSE_MV, PULSE_SECONDS, ROW_SECONDS, rowHeight, rowWidth, xOf, yOffset, type Scale } from './geometry'

// Draws one row of the strip (10 s) on a 2D context, in CSS pixels. No colour literals: the
// colours are the design tokens, read by the caller (see `readColours`).

export interface Colours {
  paper: string
  minor: string
  major: string
  trace: string
  marker: string
  window: string
}

export const TOKEN_COLOURS: Record<keyof Colours, string> = {
  paper: '--ov-ecg-paper',
  minor: '--ov-ecg-minor',
  major: '--ov-ecg-major',
  trace: '--ov-text',
  marker: '--ov-exp',
  window: '--ov-brand',
}

export function readColours(element: Element): Colours {
  const style = getComputedStyle(element)
  const out = {} as Colours
  for (const key of Object.keys(TOKEN_COLOURS) as (keyof Colours)[]) out[key] = style.getPropertyValue(TOKEN_COLOURS[key]).trim()
  return out
}

export interface RowInput {
  /** Millivolts. */
  samples: readonly number[]
  rate: number
  /** Start of the row, seconds from the start of the recording. */
  start: number
  scale: Scale
  /** The largest absolute millivolt of the whole recording: the row is tall enough for it. */
  peak: number
  /** Experimental R-peaks in seconds; absent unless the estimate is shown. */
  markers?: readonly number[]
}

function grid(ctx: CanvasRenderingContext2D, width: number, height: number, scale: Scale, colours: Colours) {
  const mm = scale.pxPerMm
  for (const [step, colour, line] of [[1, colours.minor, 1], [5, colours.major, 1.25]] as const) {
    ctx.strokeStyle = colour
    ctx.lineWidth = line
    ctx.beginPath()
    for (let x = 0; x <= width; x += step * mm) {
      ctx.moveTo(x, 0)
      ctx.lineTo(x, height)
    }
    for (let y = 0; y <= height; y += step * mm) {
      ctx.moveTo(0, y)
      ctx.lineTo(width, y)
    }
    ctx.stroke()
  }
}

/** 1 mV x 200 ms: at 10 mm/mV it is 10 mm high and 5 mm wide, at every zoom. */
function pulse(ctx: CanvasRenderingContext2D, baseline: number, scale: Scale) {
  const x0 = 2 * scale.pxPerMm
  const x1 = x0 + PULSE_SECONDS * MM_PER_SECOND * scale.pxPerMm
  const top = baseline + yOffset(scale, PULSE_MV)
  ctx.moveTo(x0 - scale.pxPerMm, baseline)
  ctx.lineTo(x0, baseline)
  ctx.lineTo(x0, top)
  ctx.lineTo(x1, top)
  ctx.lineTo(x1, baseline)
  ctx.lineTo(x1 + scale.pxPerMm, baseline)
}

export function drawRow(ctx: CanvasRenderingContext2D, input: RowInput, colours: Colours) {
  const { samples, rate, start, scale, peak, markers } = input
  const width = rowWidth(scale)
  const height = rowHeight(scale, peak)
  const baseline = height / 2
  ctx.fillStyle = colours.paper
  ctx.fillRect(0, 0, width, height)
  grid(ctx, width, height, scale, colours)

  ctx.strokeStyle = colours.trace
  ctx.lineWidth = 1.25
  ctx.lineJoin = 'round'
  ctx.beginPath()
  pulse(ctx, baseline, scale)
  ctx.stroke()
  ctx.beginPath()
  const from = Math.max(0, Math.floor(start * rate))
  const to = Math.min(samples.length - 1, Math.ceil((start + ROW_SECONDS) * rate))
  let started = false
  for (let i = from; i <= to; i++) {
    const x = xOf(scale, i / rate - start)
    const y = baseline + yOffset(scale, samples[i] ?? 0)
    if (started) ctx.lineTo(x, y)
    else ctx.moveTo(x, y)
    started = true
  }
  ctx.stroke()

  if (markers !== undefined && markers.length > 0) {
    ctx.strokeStyle = colours.marker
    ctx.lineWidth = 1
    ctx.setLineDash([4, 3])
    ctx.beginPath()
    for (const t of markers) {
      if (t < start || t > start + ROW_SECONDS) continue
      const x = xOf(scale, t - start)
      ctx.moveTo(x, 0)
      ctx.lineTo(x, height)
    }
    ctx.stroke()
    ctx.setLineDash([])
  }
}

