import { yRange } from './seriesData'

// The tile sparkline of the Overview (docs/design/mockups/src/Dashboard.dc.html): the typical
// range as a band, the smoothed line over it and the latest value as a dot. Plain SVG with
// classes and tokens. A `null` breaks the line (nothing is drawn across a gap, never through zero).

export interface SparkPoint {
  value: number | null
  low?: number | null
  high?: number | null
}

const WIDTH = 240
const HEIGHT = 56
const MARGIN = 5

const finite = (n: number | null | undefined): n is number => typeof n === 'number' && Number.isFinite(n)

export interface SparkPaths {
  line: string
  band: string
  /** Where the latest value sits, in the 240 x 56 box. */
  last: { x: number; y: number } | null
  /** Points outside the typical range, as small circles (the "attention" colour). */
  outside: { x: number; y: number }[]
}

function fixed(n: number): string {
  return n.toFixed(1)
}

export function sparkPaths(points: SparkPoint[]): SparkPaths {
  const [min, max] = yRange(points.flatMap((p) => [p.value, p.low, p.high]).filter(finite))
  const span = max - min || 1
  const x = (i: number) => (points.length < 2 ? WIDTH / 2 : MARGIN + (i / (points.length - 1)) * (WIDTH - 2 * MARGIN))
  const y = (v: number) => MARGIN + (1 - (v - min) / span) * (HEIGHT - 2 * MARGIN)

  let line = ''
  let pen = false
  const outside: SparkPaths['outside'] = []
  let last: SparkPaths['last'] = null
  points.forEach((p, i) => {
    if (!finite(p.value)) {
      pen = false
      return
    }
    line += `${pen ? 'L' : 'M'}${fixed(x(i))} ${fixed(y(p.value))}`
    pen = true
    last = { x: x(i), y: y(p.value) }
    if (finite(p.low) && finite(p.high) && (p.value < p.low || p.value > p.high)) outside.push(last)
  })

  // The band in runs, broken where the range is missing.
  let band = ''
  let run: number[] = []
  const flush = () => {
    if (run.length > 1) {
      band += run.map((i, n) => `${n === 0 ? 'M' : 'L'}${fixed(x(i))} ${fixed(y(points[i]?.high ?? 0))}`).join('')
      band += [...run].reverse().map((i) => `L${fixed(x(i))} ${fixed(y(points[i]?.low ?? 0))}`).join('') + 'Z'
    }
    run = []
  }
  points.forEach((p, i) => {
    if (finite(p.low) && finite(p.high)) run.push(i)
    else flush()
  })
  flush()
  return { line, band, last, outside }
}

interface Props {
  points: SparkPoint[]
  /** The latest value is outside the typical range: its dot is orange. */
  attention?: boolean
  /** Read out by a screen reader. Without it the sparkline is decoration: the tile says it in text. */
  label?: string
  /** A new reading just arrived: a soft halo pulses once on the last point (reduced motion: none). */
  pulse?: boolean
}

export function Sparkline({ points, attention = false, label, pulse = false }: Props) {
  const { line, band, last, outside } = sparkPaths(points)
  return (
    <svg
      className="ov-spark"
      viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`}
      role={label === undefined ? undefined : 'img'}
      aria-label={label}
      aria-hidden={label === undefined ? true : undefined}
    >
      {band === '' ? null : <path d={band} className="ov-c-band" />}
      {line === '' ? null : <path d={line} className="ov-c-line" />}
      {outside.map((o) => (
        <circle key={`${fixed(o.x)}`} cx={o.x} cy={o.y} r={2} className="ov-c-out" />
      ))}
      {last === null || !pulse ? null : <circle cx={last.x} cy={last.y} r={3.5} className="ov-c-pulse" />}
      {last === null ? null : (
        <circle cx={last.x} cy={last.y} r={3.5} className={attention ? 'ov-c-last ov-c-last--attn' : 'ov-c-last'} />
      )}
    </svg>
  )
}
