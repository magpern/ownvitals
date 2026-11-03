import { describe, expect, it } from 'vitest'
import { paintBack, paintFront, type Paint, type Projection } from './painters'
import { DAY_SECONDS, buildChartData } from './seriesData'
import { SYNTHETIC_START, syntheticWeight } from './synthetic'
import type { ChartColours } from './tokens'

// The painters against a recording context: what is drawn, where, and in which token colour.

const COLOURS: ChartColours = {
  series: 'T-series', band: 'T-band', bandEdge: 'T-edge', grid: 'T-grid', axis: 'T-axis', text: 'T-text',
  surface: 'T-surface', ground: 'T-ground', line: 'T-line', attn: 'T-attn', font: 'T-font',
}

function recorder() {
  const calls: string[] = []
  const texts: string[] = []
  const fills: unknown[] = []
  const aligns: string[] = []
  const state = {
    fillStyle: '' as string | CanvasPattern, strokeStyle: '', lineWidth: 1, globalAlpha: 1, font: '',
    textAlign: 'left' as CanvasTextAlign, textBaseline: 'alphabetic' as CanvasTextBaseline,
  }
  const ctx = new Proxy(state, {
    get(target, key: string) {
      if (key in target) return target[key as keyof typeof target]
      return (...args: unknown[]) => {
        calls.push(key)
        if (key === 'fillText') {
          texts.push(String(args[0]))
          aligns.push(`${String(args[0])}:${String(target.textAlign)}`)
        }
        if (key === 'fill' || key === 'fillRect') fills.push(target.fillStyle)
        if (key === 'measureText') return { width: 80 }
        if (key === 'arc') calls.push(`arc:${String(args[2])}`)
      }
    },
    set(target, key: string, value) {
      ;(target as Record<string, unknown>)[key] = value
      return true
    },
  }) as unknown as Paint
  return { ctx, calls, texts, fills, aligns }
}

const projection: Projection = {
  x: (t) => (t - SYNTHETIC_START) / DAY_SECONDS * 10,
  y: (v) => 400 - v * 4,
  left: 0, top: 20, width: 600, height: 300, ratio: 1,
}

describe('paintBack', () => {
  it('hatches the gap over the ground colour, and fills the band in token colours', () => {
    const data = buildChartData(syntheticWeight())
    const { ctx, fills, calls } = recorder()
    paintBack(ctx, projection, data, COLOURS, 'HATCH')
    expect(fills.slice(0, 2)).toEqual(['T-ground', 'HATCH'])
    expect(fills).toContain('T-band')
    // The band is two runs (before and after the gap), not one polygon across it.
    expect(calls.filter((c) => c === 'closePath')).toHaveLength(2)
  })
})

describe('paintFront', () => {
  const data = buildChartData(syntheticWeight(), { changePoints: [{ t: SYNTHETIC_START + 45 * DAY_SECONDS, label: 'change point · 15 Feb' }] })

  it('labels the gap with its length, and notes unconfirmed readings and the change point', () => {
    const { ctx, texts } = recorder()
    paintFront(ctx, projection, data, COLOURS, { unit: 'kg', format: (v) => v.toFixed(1) })
    expect(texts).toEqual(expect.arrayContaining(['no data', '9 days', 'unconfirmed', 'change point · 15 Feb', 'kg']))
    expect(texts.some((t) => /^\d+\.\d kg$/.test(t))).toBe(true)
  })

  it('puts the "unconfirmed" note on the left of a reading at the right edge, so it is not cut off', () => {
    const edge = buildChartData([
      { t: SYNTHETIC_START, value: 80 },
      { t: SYNTHETIC_START + 59 * DAY_SECONDS, value: 82, unconfirmed: true },
    ])
    const { ctx, aligns } = recorder()
    paintFront(ctx, projection, edge, COLOURS)
    expect(aligns).toContain('unconfirmed:right')
    const early = recorder()
    paintFront(early.ctx, projection, buildChartData([{ t: SYNTHETIC_START, value: 80 }, { t: SYNTHETIC_START + 2 * DAY_SECONDS, value: 82, unconfirmed: true }, { t: SYNTHETIC_START + 59 * DAY_SECONDS, value: 80 }]), COLOURS)
    expect(early.aligns).toContain('unconfirmed:left')
  })

  it('draws no label for a gap that is not there', () => {
    const { ctx, texts } = recorder()
    paintFront(ctx, projection, buildChartData([{ t: SYNTHETIC_START, value: 80 }, { t: SYNTHETIC_START + DAY_SECONDS, value: 81 }]), COLOURS)
    expect(texts).not.toContain('no data')
  })

  it('turns the latest dot orange outside the typical range, and never uses another colour for it', () => {
    const outside = buildChartData([
      { t: SYNTHETIC_START, value: 80, low: 78, high: 82 },
      { t: SYNTHETIC_START + DAY_SECONDS, value: 90, low: 78, high: 82 },
    ])
    const { ctx, fills } = recorder()
    paintFront(ctx, projection, outside, COLOURS)
    expect(fills.at(-1)).toBe('T-attn')
    const inside = recorder()
    paintFront(inside.ctx, projection, buildChartData([{ t: SYNTHETIC_START, value: 80, low: 78, high: 82 }]), COLOURS)
    expect(inside.fills.at(-1)).toBe('T-series')
  })
})
