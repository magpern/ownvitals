import type { MetricTile } from '../../api/overview'
import { useT } from '../../i18n'
import type { MessageKey } from '../../i18n/en'
import { useFormat } from '../../i18n/useFormat'
import { Card } from '../ui/Card'
import { DataState, type DataStateKind } from '../ui/DataState'
import { Value } from '../ui/Value'
import { dataStateKind } from '../metric/names'
import { POSITIONS, segmentTile, segmentValue, symmetryPercent, type Position } from './bodyModel'

// Arms and legs (docs/design/README.md, "Body composition"): the segmental muscle mass of the four
// positions and how far left and right differ. Descriptive only: no good or bad colour. A
// position the API does not list reads "not available", one the device cannot measure "not
// supported": never an empty row, never a zero.

/** The data state of a row; the counting state carries its progress. */
function Stateless({ kind, progress, children }: { kind: DataStateKind; progress: number; children: string }) {
  if (kind === 'importing') return <DataState kind="importing" progress={progress}>{children}</DataState>
  if (kind === 'insufficient-data') return <DataState kind="insufficient-data" progress={progress}>{children}</DataState>
  return <DataState kind={kind}>{children}</DataState>
}

function Row({ position, tile }: { position: Position; tile: MetricTile | null }) {
  const t = useT()
  const f = useFormat()
  const value = segmentValue(tile)
  const label = t(`body.segment.${position}` as MessageKey)
  const state = tile === null ? 'not_available' : tile.state
  return (
    <li className="ov-segment" data-position={position}>
      <span className="ov-segment__name">{label}</span>
      {value === null ? (
        <Stateless kind={dataStateKind(state)} progress={tile?.progressPercent ?? 0}>
          {t(`overview.tile.detail.${tile === null ? 'not_available' : state}` as MessageKey)}
        </Stateless>
      ) : (
        <Value value={f.quantity(value, tile?.unit ?? 'kg', 1)} unit={f.unit(tile?.unit ?? 'kg')} />
      )}
    </li>
  )
}

function Difference({ pair, left, right }: { pair: 'arms' | 'legs'; left: number | null; right: number | null }) {
  const t = useT()
  const f = useFormat()
  const percent = symmetryPercent(left, right)
  const name = t(`body.segment.${pair}` as MessageKey)
  return (
    <p className="ov-small ov-muted">
      {percent === null
        ? t('body.segment.noDifference', { pair: name })
        : t('body.segment.difference', { pair: name, percent: f.number(percent, 1) })}
    </p>
  )
}

export function SegmentalCard({ tiles }: { tiles: readonly MetricTile[] }) {
  const t = useT()
  const by = (position: Position) => segmentTile(tiles, position)
  const v = (position: Position) => segmentValue(by(position))
  return (
    <Card level={3} title={t('body.segment.title')}>
      <ul className="ov-segments" aria-label={t('body.segment.title')}>
        {POSITIONS.map((p) => (
          <Row key={p} position={p} tile={by(p)} />
        ))}
      </ul>
      <Difference pair="arms" left={v('left_arm')} right={v('right_arm')} />
      <Difference pair="legs" left={v('left_leg')} right={v('right_leg')} />
    </Card>
  )
}
