// The phone's range bar (docs/design/mockups/src/Onboarding.dc.html, "Phone overview"): where
// today sits in your typical range, in place of a chart. The band is the typical range, the dot is
// the latest value (orange when it is outside, never red), and the line below says it in words.

export type RangePosition = 'within' | 'above' | 'below'

export function rangePosition(value: number, low: number, high: number): RangePosition {
  if (value > high) return 'above'
  if (value < low) return 'below'
  return 'within'
}

/** "Within your typical range 78.3–79.5", "Above your typical range 59–63". */
export function rangeCaption(position: RangePosition, range: string): string {
  const words = { within: 'Within', above: 'Above', below: 'Below' }[position]
  return `${words} your typical range ${range}`
}

/** The bar's scale in percent: the band, with room on both sides, and the value inside it. */
export function rangeScale(value: number, low: number, high: number): { band: [number, number]; value: number } {
  const span = Math.max(high - low, Number.EPSILON)
  const pad = span * 0.6
  const min = Math.min(low, value) - pad
  const max = Math.max(high, value) + pad
  const pct = (v: number) => ((v - min) / (max - min)) * 100
  return { band: [pct(low), pct(high) - pct(low)], value: pct(value) }
}

interface Props {
  value: number
  low: number
  high: number
  /** How a number is written in the caption (the metric's decimals). */
  format?: (n: number) => string
}

export function RangeBar({ value, low, high, format = String }: Props) {
  const position = rangePosition(value, low, high)
  const scale = rangeScale(value, low, high)
  const caption = rangeCaption(position, `${format(low)}–${format(high)}`)
  const attention = position !== 'within'
  const pct = (n: number) => `${n.toFixed(1)}%`
  return (
    <div className="ov-rangebar" data-position={position}>
      <svg className="ov-rangebar__bar" role="img" aria-label={caption} width="100%" height="14">
        <rect x="0" y="0" width="100%" height="14" rx="7" className="ov-rangebar__track" />
        <rect x={pct(scale.band[0])} y="0" width={pct(scale.band[1])} height="14" rx="7" className="ov-c-band-box" />
        <circle cx={pct(scale.value)} cy="7" r="6" className={attention ? 'ov-c-last ov-c-last--attn' : 'ov-c-last'} />
      </svg>
      <span className={attention ? 'ov-rangebar__caption ov-rangebar__caption--attn' : 'ov-rangebar__caption'}>{caption}</span>
    </div>
  )
}
