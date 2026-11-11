import type { MetricTile } from '../../api/overview'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { convertQuantity } from '../../lib/display'
import { deltaOf } from './deltaModel'

// "Change against your own median" chip of a tile (docs/design/motion.md, item 3): arrow, signed
// number, what it is compared with. Neutral series blue, or the attention orange when the latest
// value is outside the typical range; never green or red (a change is not good or bad by itself).
// It fades in after the number (CSS only).

interface Props {
  tile: MetricTile
  digits: number
  attention: boolean
}

const ARROW = { up: '↑', down: '↓', flat: '→' } as const

export function DeltaChip({ tile, digits, attention }: Props) {
  const t = useT()
  const f = useFormat()
  const delta = deltaOf(tile)
  if (delta === null || tile.latest === null) return null
  // Convert both ends, then subtract: a difference must not go through an offset (Celsius to Fahrenheit).
  const shown = convertQuantity(f.settings, tile.latest.value, tile.unit).value - convertQuantity(f.settings, tile.latest.value - delta.amount, tile.unit).value
  const magnitude = f.number(Math.abs(shown), digits)
  const sign = delta.direction === 'flat' ? '' : shown > 0 ? '+' : '−'
  const text = t('overview.delta.vs', { change: `${sign}${magnitude}`, days: delta.days })
  return (
    <p className={attention ? 'ov-delta ov-delta--attn' : 'ov-delta'} data-direction={delta.direction}>
      <span aria-hidden="true">{ARROW[delta.direction]} </span>
      {text}
    </p>
  )
}
