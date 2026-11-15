import { describe, expect, it } from 'vitest'
import { drawRow, type Colours } from './drawStrip'
import { bandpass } from './filter'
import { MM_PER_SECOND, PX_PER_MM, rowHeightMm, scaleOf, xOf } from './geometry'

// Synthetic signals only. The renderer is checked through the calls it makes on a fake 2D context.

const COLOURS: Colours = { paper: 'p', minor: 'n', major: 'j', trace: 't', marker: 'm', window: 'w' }

interface Fake {
  ctx: CanvasRenderingContext2D
  paths: { style: string; move: [number, number][]; line: [number, number][] }[]
  dashes: number
}

function fake(): Fake {
  const out: Fake = { ctx: null as unknown as CanvasRenderingContext2D, paths: [], dashes: 0 }
  let current = { style: '', move: [] as [number, number][], line: [] as [number, number][] }
  const state = { strokeStyle: '' }
  const ctx = {
    get strokeStyle() {
      return state.strokeStyle
    },
    set strokeStyle(v: string) {
      state.strokeStyle = v
    },
    lineWidth: 0,
    lineJoin: '',
    fillStyle: '',
    beginPath: () => {
      current = { style: state.strokeStyle, move: [], line: [] }
      out.paths.push(current)
    },
    moveTo: (x: number, y: number) => current.move.push([x, y]),
    lineTo: (x: number, y: number) => current.line.push([x, y]),
    setLineDash: (d: number[]) => {
      if (d.length > 0) out.dashes++
    },
    fillRect: () => {},
    stroke: () => {},
  }
  out.ctx = ctx as unknown as CanvasRenderingContext2D
  return out
}

/** The trace path: the last path drawn with the trace colour (here: the one with many points). */
function trace(f: Fake) {
  return f.paths.filter((p) => p.style === COLOURS.trace && p.line.length !== 5).reduce((a, b) => (b.line.length > a.line.length ? b : a))
}

describe.each([1, 1.5, 2])('the strip at zoom %s', (zoom) => {
  const scale = scaleOf(zoom, 10)
  const mm = PX_PER_MM * zoom
  const rate = 100
  // 0 mV for 1 s, then 1 mV.
  const samples = Array.from({ length: 1000 }, (_, i) => (i >= rate ? 1 : 0))

  it('draws a 1 mV step as 10 mm and 1 s as 25 mm', () => {
    const f = fake()
    drawRow(f.ctx, { samples, rate, start: 0, scale, peak: 1 }, COLOURS)
    const t = trace(f)
    const points = [t.move[0] as [number, number], ...t.line]
    const before = points[rate - 1] as [number, number]
    const at1s = points[rate] as [number, number]
    const after = points[rate + 1] as [number, number]
    // 1 s of signal is 25 mm along the paper.
    expect(at1s[0] - (points[0] as [number, number])[0]).toBeCloseTo(MM_PER_SECOND * mm, 6)
    // The step is 10 mm high.
    expect(before[1] - after[1]).toBeCloseTo(10 * mm, 6)
  })

  it('draws the calibration pulse 10 mm high and 5 mm wide', () => {
    const f = fake()
    drawRow(f.ctx, { samples, rate, start: 0, scale, peak: 1 }, COLOURS)
    const pulse = f.paths.find((p) => p.line.length === 5)
    expect(pulse).toBeDefined()
    const [, up = [0, 0], across = [0, 0], down = [0, 0]] = pulse?.line ?? []
    const base = pulse?.line[0]?.[1] ?? 0
    expect(base - up[1]).toBeCloseTo(10 * mm, 6)
    expect(across[0] - up[0]).toBeCloseTo(5 * mm, 6)
    expect(down[1]).toBeCloseTo(base, 6)
  })
})

describe('gain and clipping', () => {
  it('a 5 mm/mV row draws the step 5 mm high, and it is labelled as a gain, not a fit', () => {
    const f = fake()
    const samples = Array.from({ length: 300 }, (_, i) => (i >= 100 ? 1 : 0))
    drawRow(f.ctx, { samples, rate: 100, start: 0, scale: scaleOf(1, 5), peak: 1 }, COLOURS)
    const t = trace(f)
    const pts = [t.move[0] as [number, number], ...t.line]
    expect((pts[99] as [number, number])[1] - (pts[101] as [number, number])[1]).toBeCloseTo(5 * PX_PER_MM, 6)
  })

  it('makes the row taller for a large signal instead of clipping it', () => {
    expect(rowHeightMm(0.5, 10)).toBe(40)
    const tall = rowHeightMm(4, 10) // 4 mV at 10 mm/mV = 40 mm each way
    expect(tall).toBeGreaterThanOrEqual(2 * (4 * 10))
    expect(rowHeightMm(4, 5)).toBeLessThan(tall)
  })

  it('draws markers only when given them', () => {
    const a = fake()
    drawRow(a.ctx, { samples: [0, 0, 0], rate: 100, start: 0, scale: scaleOf(1, 10), peak: 0 }, COLOURS)
    expect(a.dashes).toBe(0)
    const b = fake()
    drawRow(b.ctx, { samples: [0, 0, 0], rate: 100, start: 0, scale: scaleOf(1, 10), peak: 0, markers: [0.01] }, COLOURS)
    expect(b.dashes).toBe(1)
  })

  it('places a marker by its sample position, not as seconds', () => {
    const f = fake()
    // Sample 500 at 500 Hz is 1 s: 25 mm from the row start. Read as seconds it would be off the row.
    drawRow(f.ctx, { samples: [0, 0, 0], rate: 500, start: 0, scale: scaleOf(1, 10), peak: 0, markers: [500] }, COLOURS)
    const dashed = f.paths.find((p) => p.style === COLOURS.marker)
    expect(dashed?.move[0]?.[0]).toBeCloseTo(xOf(scaleOf(1, 10), 1), 6)
  })
})

describe('the display filter', () => {
  it('removes a constant offset and keeps a 10 Hz tone', () => {
    const rate = 250
    const x = Array.from({ length: 2500 }, (_, i) => 2 + Math.sin((2 * Math.PI * 10 * i) / rate))
    const y = bandpass(x, rate)
    const mid = y.slice(1000, 1500)
    expect(Math.abs(mid.reduce((a, b) => a + b, 0) / mid.length)).toBeLessThan(0.05)
    expect(Math.max(...mid)).toBeGreaterThan(0.9)
  })
})
